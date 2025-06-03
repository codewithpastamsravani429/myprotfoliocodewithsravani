from core.agents.base import BaseAgent
from core.agents.convo import AgentConvo
from core.agents.response import AgentResponse, ResponseType
from core.config import SPEC_WRITER_AGENT_NAME
from core.config.actions import SPEC_CHANGE_FEATURE_STEP_NAME, SPEC_CHANGE_STEP_NAME, SPEC_CREATE_STEP_NAME
from core.db.models import Complexity
from core.db.models.project_state import IterationStatus
from core.llm.parser import StringParser
from core.log import get_logger
from core.telemetry import telemetry
from core.ui.base import ProjectStage

# If the project description is less than this, perform an analysis using LLM
ANALYZE_THRESHOLD = 1500
# URL to the wiki page with tips on how to write a good project description
INITIAL_PROJECT_HOWTO_URL = (
    "https://github.com/Pythagora-io/gpt-pilot/wiki/How-to-write-a-good-initial-project-description"
)
log = get_logger(__name__)


class SpecWriter(BaseAgent):
    agent_type = "spec-writer"
    display_name = "Spec Writer"

    async def run(self) -> AgentResponse:
        current_iteration = self.current_state.current_iteration
        if current_iteration is not None and current_iteration.get("status") == IterationStatus.NEW_FEATURE_REQUESTED:
            return await self.update_spec(iteration_mode=True)
        elif self.prev_response and self.prev_response.type == ResponseType.UPDATE_SPECIFICATION:
            return await self.update_spec(iteration_mode=False)
        else:
            return await self.initialize_spec()

    async def initialize_spec(self) -> AgentResponse:
        self.next_state.action = SPEC_CREATE_STEP_NAME

        await self.ui.send_project_stage({"stage": ProjectStage.PROJECT_DESCRIPTION})

        user_description = await self.ask_question(
            "Please describe the app you want to build.",
            allow_empty=False,
            full_screen=True,
        )
        description = user_description.text.strip()
        complexity = await self.check_prompt_complexity(description)

        llm = self.get_llm(SPEC_WRITER_AGENT_NAME, stream_output=True)
        convo = AgentConvo(self).template(
            "build_full_specification",
            initial_prompt=description,
            auth=(self.current_state.knowledge_base or {}).get("auth", False),
        )

        await self.ui.start_important_stream()
        llm_assisted_description = await llm(convo)

        while True:
            user_done_with_description = await self.ask_question(
                "Are you satisfied with the project description?",
                buttons={
                    "yes": "Yes",
                    "no": "No, I want to add more details",
                },
                default="yes",
                buttons_only=True,
            )

            if user_done_with_description.button == "yes":
                break

            user_add_to_spec = await self.ask_question(
                "What would you like to add?",
                allow_empty=False,
            )

            convo = convo.template("add_to_specification", user_message=user_add_to_spec.text.strip())

            if len(convo.messages) > 6:
                convo.slice(1, 4)

            await self.ui.start_important_stream()
            llm_assisted_description = await llm(convo)
            convo = convo.template(
                "build_full_specification",
                auth=(self.current_state.knowledge_base or {}).get("auth", False),
                initial_prompt=llm_assisted_description.strip(),
            )

        # if we reload the project from the 1st project state, state_manager.template will be None
        if self.state_manager.template:
            self.state_manager.template["description"] = llm_assisted_description
        else:
            # if we do not set this and reload the project, we will load the "old" project description we entered before reload
            self.next_state.epics[0]["description"] = llm_assisted_description

        self.next_state.specification = self.current_state.specification.clone()
        self.next_state.specification.original_description = description
        self.next_state.specification.description = llm_assisted_description
        self.next_state.specification.complexity = complexity

        telemetry.set("initial_prompt", description)
        telemetry.set("updated_prompt", llm_assisted_description)
        telemetry.set("is_complex_app", complexity != Complexity.SIMPLE)

        await self.ui.send_project_description(
            {
                "project_description": llm_assisted_description,
                "project_type": self.current_state.branch.project.project_type,
            }
        )

        await telemetry.trace_code_event(
            "project-description",
            {
                "complexity": complexity,
                "initial_prompt": description,
                "llm_assisted_prompt": llm_assisted_description,
            },
        )

        self.next_state.epics = [
            {
                "id": self.current_state.epics[0]["id"],
                "name": "Build frontend",
                "source": "frontend",
                "description": llm_assisted_description,
                "messages": [],
                "summary": None,
                "completed": False,
            }
        ]

        return AgentResponse.done(self)

    async def update_spec(self, iteration_mode) -> AgentResponse:
        if iteration_mode:
            self.next_state.action = SPEC_CHANGE_FEATURE_STEP_NAME
            feature_description = self.current_state.current_iteration["user_feedback"]
        else:
            self.next_state.action = SPEC_CHANGE_STEP_NAME
            feature_description = self.prev_response.data["description"]

        await self.send_message(
            f"Making the following changes to project specification:\n\n{feature_description}\n\nUpdated project specification:"
        )
        llm = self.get_llm(SPEC_WRITER_AGENT_NAME, stream_output=True)
        convo = AgentConvo(self).template("add_new_feature", feature_description=feature_description)
        llm_response: str = await llm(convo, temperature=0, parser=StringParser())
        updated_spec = llm_response.strip()
        await self.ui.generate_diff(
            "project_specification", self.current_state.specification.description, updated_spec, source=self.ui_source
        )
        user_response = await self.ask_question(
            "Do you accept these changes to the project specification?",
            buttons={"yes": "Yes", "no": "No"},
            default="yes",
            buttons_only=True,
        )
        await self.ui.close_diff()

        if user_response.button == "yes":
            self.next_state.specification = self.current_state.specification.clone()
            self.next_state.specification.description = updated_spec
            telemetry.set("updated_prompt", updated_spec)

        if iteration_mode:
            self.next_state.current_iteration["status"] = IterationStatus.FIND_SOLUTION
            self.next_state.flag_iterations_as_modified()
        else:
            complexity = await self.check_prompt_complexity(feature_description)
            self.next_state.current_epic["complexity"] = complexity

        return AgentResponse.done(self)

    async def check_prompt_complexity(self, prompt: str) -> str:
        is_feature = self.current_state.epics and len(self.current_state.epics) > 2
        await self.send_message("Checking the complexity of the prompt ...")
        llm = self.get_llm(SPEC_WRITER_AGENT_NAME)
        convo = AgentConvo(self).template(
            "prompt_complexity",
            prompt=prompt,
            is_feature=is_feature,
        )
        llm_response: str = await llm(convo, temperature=0, parser=StringParser())
        log.info(f"Complexity check response: {llm_response}")
        return llm_response.lower()

    async def analyze_spec(self, spec: str) -> str:
        msg = (
            "Your project description seems a bit short. "
            "The better you can describe the project, the better Pythagora will understand what you'd like to build.\n\n"
            f"Here are some tips on how to better describe the project: {INITIAL_PROJECT_HOWTO_URL}\n\n"
            "Let's start by refining your project idea:"
        )
        await self.send_message(msg)

        llm = self.get_llm(SPEC_WRITER_AGENT_NAME, stream_output=True)
        convo = AgentConvo(self).template("ask_questions").user(spec)
        n_questions = 0
        n_answers = 0

        while True:
            response: str = await llm(convo)
            if len(response) > 500:
                # The response is too long for it to be a question, assume it's the updated spec
                confirm = await self.ask_question(
                    ("Would you like to change or add anything? Write it out here."),
                    allow_empty=True,
                    buttons={"continue": "No thanks, the spec looks good"},
                )
                if confirm.cancelled or confirm.button == "continue" or confirm.text == "":
                    updated_spec = response.strip()
                    await telemetry.trace_code_event(
                        "spec-writer-questions",
                        {
                            "num_questions": n_questions,
                            "num_answers": n_answers,
                            "new_spec": updated_spec,
                        },
                    )
                    return updated_spec
                convo.user(confirm.text)

            else:
                convo.assistant(response)

                n_questions += 1
                user_response = await self.ask_question(
                    response,
                    buttons={"skip": "Skip this question", "skip_all": "No more questions"},
                    verbose=False,
                )
                if user_response.cancelled or user_response.button == "skip_all":
                    convo.user(
                        "This is enough clarification, you have all the information. "
                        "Please output the spec now, without additional comments or questions."
                    )
                    response: str = await llm(convo)
                    confirm = await self.ask_question(
                        ("Would you like to change or add anything? Write it out here."),
                        allow_empty=True,
                        buttons={"continue": "No thanks, the spec looks good"},
                    )
                    if confirm.cancelled or confirm.button == "continue" or confirm.text == "":
                        updated_spec = response.strip()
                        await telemetry.trace_code_event(
                            "spec-writer-questions",
                            {
                                "num_questions": n_questions,
                                "num_answers": n_answers,
                                "new_spec": updated_spec,
                            },
                        )
                        return updated_spec
                    convo.user(confirm.text)
                    continue

                n_answers += 1
                if user_response.button == "skip":
                    convo.user("Skip this question.")
                    continue
                else:
                    convo.user(user_response.text)

    async def review_spec(self, desc: str, spec: str) -> str:
        convo = AgentConvo(self).template("review_spec", desc=desc, spec=spec)
        llm = self.get_llm(SPEC_WRITER_AGENT_NAME)
        llm_response: str = await llm(convo, temperature=0)
        additional_info = llm_response.strip()
        if additional_info and len(additional_info) > 6:
            spec += "\n\nAdditional info/examples:\n\n" + additional_info
            await self.send_message(f"\n\nAdditional info/examples:\n\n {additional_info}")

        return spec
