from urllib.parse import urljoin

import httpx

from core.agents.base import BaseAgent
from core.agents.convo import AgentConvo
from core.agents.git import GitMixin
from core.agents.mixins import FileDiffMixin
from core.agents.response import AgentResponse
from core.cli.helpers import capture_exception
from core.config import FRONTEND_AGENT_NAME, SWAGGER_EMBEDDINGS_API
from core.config.actions import (
    FE_CHANGE_REQ,
    FE_CONTINUE,
    FE_DONE_WITH_UI,
    FE_ITERATION,
    FE_ITERATION_DONE,
    FE_START,
)
from core.llm.parser import DescriptiveCodeBlockParser
from core.log import get_logger
from core.telemetry import telemetry
from core.ui.base import ProjectStage

log = get_logger(__name__)


class Frontend(FileDiffMixin, GitMixin, BaseAgent):
    agent_type = "frontend"
    display_name = "Frontend"

    async def run(self) -> AgentResponse:
        if not self.current_state.epics[0]["messages"]:
            finished = await self.start_frontend()
        elif self.next_state.epics[-1].get("file_paths_to_remove_mock"):
            finished = await self.remove_mock()
            if finished is None:
                return AgentResponse.exit(self)
        elif not self.next_state.epics[-1].get("fe_iteration_done"):
            finished = await self.continue_frontend()
        else:
            await self.set_app_details()
            finished = await self.iterate_frontend()
            if finished is None:
                return AgentResponse.exit(self)

        return await self.end_frontend_iteration(finished)

    async def start_frontend(self):
        """
        Starts the frontend of the app.
        """
        self.next_state.action = FE_START
        await self.send_message("Building the frontend... This may take a couple of minutes")

        llm = self.get_llm(FRONTEND_AGENT_NAME)
        convo = AgentConvo(self).template(
            "build_frontend",
            summary=self.state_manager.template["template"].get_summary()
            if self.state_manager.template is not None
            else self.current_state.specification.template_summary,
            description=self.state_manager.template["description"]
            if self.state_manager.template is not None
            else self.next_state.epics[0]["description"],
            user_feedback=None,
            first_time_build=True,
            api_model_definitions=self.current_state.knowledge_base.get("docs", {}).get("definitions", None),
        )
        response = await llm(convo, parser=DescriptiveCodeBlockParser())
        response_blocks = response.blocks
        convo.assistant(response.original_response)

        # Await the template task if it's not done yet
        if self.state_manager.async_tasks:
            if not self.state_manager.async_tasks[-1].done():
                await self.state_manager.async_tasks[-1]
            self.state_manager.async_tasks = []

        await self.process_response(response_blocks)

        self.next_state.epics[-1]["messages"] = convo.messages
        self.next_state.epics[-1]["fe_iteration_done"] = (
            "done" in response.original_response[-20:].lower().strip() or len(convo.messages) > 11
        )
        self.next_state.flag_epics_as_modified()

        return False

    async def continue_frontend(self):
        """
        Continues building the frontend of the app after the initial user input.
        """
        self.next_state.action = FE_CONTINUE
        await self.ui.send_project_stage({"stage": ProjectStage.CONTINUE_FRONTEND})
        await self.send_message("Continuing to build UI... This may take a couple of minutes")

        llm = self.get_llm(FRONTEND_AGENT_NAME)
        convo = AgentConvo(self)
        convo.messages = self.current_state.epics[0]["messages"]
        convo.user(
            "Ok, now think carefully about your previous response. If the response ends by mentioning something about continuing with the implementation, continue but don't implement any files that have already been implemented. If your last response doesn't end by mentioning continuing, respond only with `DONE` and with nothing else."
        )

        response = await llm(convo, parser=DescriptiveCodeBlockParser())
        response_blocks = response.blocks
        convo.assistant(response.original_response)

        await self.process_response(response_blocks)

        self.next_state.epics[-1]["messages"] = convo.messages
        self.next_state.epics[-1]["fe_iteration_done"] = (
            "done" in response.original_response[-20:].lower().strip() or len(convo.messages) > 15
        )
        self.next_state.flag_epics_as_modified()

        return False

    async def iterate_frontend(self):
        """
        Iterates over the frontend.

        :return: True if the frontend is fully built, False otherwise.
        """
        frontend_only = self.current_state.branch.project.project_type == "swagger"
        self.next_state.action = FE_ITERATION
        # update the pages in the knowledge base
        await self.state_manager.update_implemented_pages_and_apis()

        await self.ui.send_project_stage({"stage": ProjectStage.ITERATE_FRONTEND})

        answer = await self.ask_question(
            "Do you want to change anything or report a bug?" if frontend_only else FE_CHANGE_REQ,
            buttons={"yes": "I'm done building the UI"} if not frontend_only else None,
            default="yes",
            extra_info="restart_app/collect_logs",
            placeholder='For example, "I don\'t see anything when I open http://localhost:5173/" or "Nothing happens when I click on the NEW PROJECT button"',
        )

        if answer.button == "yes":
            answer = await self.ask_question(
                FE_DONE_WITH_UI,
                buttons={
                    "yes": "Yes, let's build the backend",
                    "no": "No, continue working on the UI",
                },
                buttons_only=True,
                default="yes",
            )

            if answer.button == "yes":
                return True
            else:
                return False

        await self.send_message("Implementing the changes you suggested...")

        llm = self.get_llm(FRONTEND_AGENT_NAME)

        relevant_api_documentation = None

        if frontend_only:
            convo = AgentConvo(self).template(
                "is_relevant_for_docs_search",
                user_feedback=answer.text,
            )

            response = await llm(convo)
            if str(response).lower() == "yes":
                error = None
                for attempt in range(3):
                    try:
                        url = urljoin(SWAGGER_EMBEDDINGS_API, "rag/search")
                        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport()) as client:
                            resp = await client.post(
                                url,
                                json={"text": answer.text, "project_id": str(self.state_manager.project.id)},
                                headers={"Authorization": f"Bearer {self.state_manager.get_access_token()}"},
                            )

                            if resp.status_code in [200]:
                                relevant_api_documentation = "\n".join(item["content"] for item in resp.json())
                                break
                            elif resp.status_code in [401, 403]:
                                access_token = await self.ui.send_token_expired()
                                self.state_manager.update_access_token(access_token)
                            else:
                                try:
                                    error = resp.json()["error"]
                                except Exception as e:
                                    error = e
                                log.warning(f"Failed to fetch from RAG service: {error}")
                                await self.send_message(
                                    f"Couldn't find any relevant API documentation. Retrying... \nError: {error}"
                                )

                    except Exception as e:
                        error = e
                        capture_exception(e)
                        log.warning(f"Failed to fetch from RAG service: {e}", exc_info=True)
                if error:
                    await self.send_message(f"Please try reloading the project. \nError: {error}")
                    return None

        llm = self.get_llm(FRONTEND_AGENT_NAME, stream_output=True)

        convo = AgentConvo(self).template(
            "build_frontend",
            description=self.current_state.epics[0]["description"],
            user_feedback=answer.text,
            relevant_api_documentation=relevant_api_documentation,
            first_time_build=False,
            api_model_definitions=self.current_state.knowledge_base.get("docs", {}).get("definitions", None),
        )

        response = await llm(convo, parser=DescriptiveCodeBlockParser())

        await self.process_response(response.blocks)

        return False

    async def end_frontend_iteration(self, finished: bool) -> AgentResponse:
        """
        Ends the frontend iteration.

        :param finished: Whether the frontend is fully built.
        :return: AgentResponse.done(self)
        """
        if finished:
            # TODO Add question if user app is fully finished
            self.next_state.action = FE_ITERATION_DONE

            self.next_state.complete_epic()
            await telemetry.trace_code_event(
                "frontend-finished",
                {
                    "description": self.current_state.epics[0]["description"],
                    "messages": self.current_state.epics[0]["messages"],
                },
            )

            if self.state_manager.git_available and self.state_manager.git_used:
                await self.git_commit(commit_message="Frontend finished")

            inputs = []
            for file in self.current_state.files:
                if not file.content:
                    continue
                input_required = self.state_manager.get_input_required(file.content.content, file.path)
                if input_required:
                    inputs += [{"file": file.path, "line": line} for line in input_required]

            if inputs:
                return AgentResponse.input_required(self, inputs)

        return AgentResponse.done(self)

    async def process_response(self, response_blocks: list, removed_mock: bool = False) -> list[str]:
        """
        Processes the response blocks from the LLM.

        :param response_blocks: The response blocks from the LLM.
        :return: AgentResponse.done(self)
        """
        for block in response_blocks:
            description = block.description.strip()
            content = block.content.strip()

            # Split description into lines and check the last line for file path
            description_lines = description.split("\n")
            last_line = description_lines[-1].strip()

            if "file:" in last_line:
                # Extract file path from the last line - get everything after "file:"
                file_path = last_line[last_line.index("file:") + 5 :].strip()
                file_path = file_path.strip("\"'`")
                # Skip empty file paths
                if file_path.strip() == "":
                    continue
                new_content = content
                old_content = self.current_state.get_file_content_by_path(file_path)
                n_new_lines, n_del_lines = self.get_line_changes(old_content, new_content)
                await self.ui.send_file_status(file_path, "done", source=self.ui_source)
                await self.ui.generate_diff(
                    file_path, old_content, new_content, n_new_lines, n_del_lines, source=self.ui_source
                )
                if not removed_mock and self.current_state.branch.project.project_type == "swagger":
                    if "client/src/api" in file_path:
                        if not self.next_state.epics[-1].get("file_paths_to_remove_mock"):
                            self.next_state.epics[-1]["file_paths_to_remove_mock"] = []
                        self.next_state.epics[-1]["file_paths_to_remove_mock"].append(file_path)

                await self.state_manager.save_file(file_path, new_content)

            elif "command:" in last_line:
                # Split multiple commands and execute them sequentially
                commands = content.strip().split("\n")
                for command in commands:
                    command = command.strip()
                    if command:
                        # Add "cd client" prefix if not already present
                        if not command.startswith("cd "):
                            command = f"cd client && {command}"
                        if "run start" in command or "run dev" in command:
                            continue

                        # if command is cd client && some_command client/ -> won't work, we need to remove client/ after &&
                        prefix, cmd_part = command.split("&&", 1)
                        cmd_part = cmd_part.strip().replace("client/", "")
                        command = f"{prefix} && {cmd_part}"

                        await self.send_message(f"Running command: `{command}`...")
                        await self.process_manager.run_command(command)
            else:
                log.info(f"Unknown block description: {description}")

        return AgentResponse.done(self)

    async def remove_mock(self):
        """
        Remove mock API from the backend and replace it with api endpoints defined in the external documentation
        """
        new_file_paths = self.current_state.epics[-1]["file_paths_to_remove_mock"]
        llm = self.get_llm(FRONTEND_AGENT_NAME)

        for file_path in new_file_paths:
            old_content = self.current_state.get_file_content_by_path(file_path)

            convo = AgentConvo(self).template("create_rag_query", file_content=old_content)
            topics = await llm(convo)

            if topics != "None":
                error = None
                for attempt in range(3):
                    try:
                        url = urljoin(SWAGGER_EMBEDDINGS_API, "rag/search")
                        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport()) as client:
                            resp = await client.post(
                                url,
                                json={"text": topics, "project_id": str(self.state_manager.project.id)},
                                headers={"Authorization": f"Bearer {self.state_manager.get_access_token()}"},
                            )
                            if resp.status_code == 200:
                                resp_json = resp.json()
                                relevant_api_documentation = "\n".join(item["content"] for item in resp_json)

                                referencing_files = await self.state_manager.get_referencing_files(
                                    self.current_state, file_path
                                )

                                convo = AgentConvo(self).template(
                                    "remove_mock",
                                    relevant_api_documentation=relevant_api_documentation,
                                    file_content=old_content,
                                    file_path=file_path,
                                    referencing_files=referencing_files,
                                    lines=len(old_content.splitlines()),
                                )

                                response = await llm(convo, parser=DescriptiveCodeBlockParser())
                                response_blocks = response.blocks
                                convo.assistant(response.original_response)
                                await self.process_response(response_blocks, removed_mock=True)
                                self.next_state.epics[-1]["file_paths_to_remove_mock"].remove(file_path)
                                break
                            elif resp.status_code in [401, 403]:
                                access_token = await self.ui.send_token_expired()
                                self.state_manager.update_access_token(access_token)
                            else:
                                try:
                                    error = resp.json()["error"]
                                except Exception as e:
                                    error = e
                                log.warning(f"Failed to fetch from RAG service: {error}")
                                await self.send_message(
                                    f"I couldn't find any relevant API documentation. Retrying... \nError: {error}"
                                )
                    except Exception as e:
                        capture_exception(e)
                        log.warning(f"Failed to fetch from RAG service: {e}", exc_info=True)
                if error:
                    await self.send_message(f"Please try reloading the project. \nError: {error}")
                    return None

        return False

    async def set_app_details(self):
        """
        Sets the app details.
        """
        command = "npm run start"
        app_link = "http://localhost:5173"

        self.next_state.run_command = command
        # todo store app link and send whenever we are sending run_command
        # self.next_state.app_link = app_link
        await self.ui.send_run_command(command)
        await self.ui.send_app_link(app_link)
