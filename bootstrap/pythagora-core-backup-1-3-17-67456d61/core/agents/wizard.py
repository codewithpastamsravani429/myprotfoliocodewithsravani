import asyncio
import json
import secrets
from json import JSONDecodeError
from urllib.parse import urljoin
from uuid import uuid4

import httpx
import yaml

from core.agents.base import BaseAgent
from core.agents.response import AgentResponse
from core.cli.helpers import capture_exception
from core.config import SWAGGER_EMBEDDINGS_API
from core.config.actions import FE_INIT
from core.log import get_logger
from core.telemetry import telemetry
from core.templates.registry import PROJECT_TEMPLATES
from core.ui.base import ProjectStage

log = get_logger(__name__)


class Wizard(BaseAgent):
    agent_type = "wizard"
    display_name = "Wizard"

    def load_docs(self, docs: str) -> dict[str, any]:
        try:
            return json.loads(docs)
        except JSONDecodeError:
            try:
                return yaml.safe_load(docs)
            except Exception as e:
                log.error(f"An error occurred: {str(e)}")
                return {}

    def get_auth_data(self, docs: dict[str, any]) -> dict[str, any]:
        auth_methods = {}
        if "openapi" in docs and docs["openapi"].startswith("3."):
            if "components" in docs and "securitySchemes" in docs["components"]:
                auth_methods["types"] = [details["type"] for details in docs["components"]["securitySchemes"].values()]
            auth_methods["api_version"] = 3
            auth_methods["external_api_url"] = docs.get("servers", [{}])[0].get("url", "https://api.example.com")

        elif "swagger" in docs and docs["swagger"].startswith("2."):
            if "securityDefinitions" in docs:
                auth_methods["types"] = [details["type"] for details in docs["securityDefinitions"].values()]
            auth_methods["api_version"] = 2
            scheme = docs.get("schemes", ["https"])[0] + "://"
            host = docs.get("host", "api.example.com")
            base_path = docs.get("basePath", "")
            auth_methods["external_api_url"] = scheme + host + base_path
        return auth_methods

    async def run(self) -> AgentResponse:
        success = await self.init_frontend()
        if not success:
            return AgentResponse.exit(self)
        return AgentResponse.done(self)

    async def init_frontend(self) -> bool:
        """
        Sets up the frontend

        :return: AgentResponse.done(self)
        """
        self.next_state.action = FE_INIT
        self.state_manager.template = {}
        options = {}
        auth_data = {}

        if self.state_manager.project.project_type == "swagger":
            while True:
                try:
                    docs = await self.ask_question(
                        "Paste the OpenAPI/Swagger JSON or YAML docs here",
                        allow_empty=False,
                        verbose=True,
                    )
                    content = self.load_docs(docs.text.strip())

                    if "paths" not in content:
                        await self.send_message("Please provide a valid input.")
                        continue

                    auth_data = self.get_auth_data(content)
                    if auth_data == {}:
                        await self.send_message("Please provide a valid input.")
                        continue

                    options["external_api_url"] = auth_data["external_api_url"]
                    success = await self.upload_docs(docs.text.strip())
                    if not success:
                        await self.send_message("Please try creating a new project.")
                        return False
                    else:
                        break
                except Exception as e:
                    log.debug(f"An error occurred: {str(e)}")
                    await self.send_message("Please provide a valid input.")
                    continue

            while True:
                auth_type_question = await self.ask_question(
                    "Which authentication method does your backend use?",
                    buttons={
                        "none": "No authentication",
                        "api_key": "API Key",
                        "bearer": "HTTP Bearer (coming soon)",
                        "open_id_connect": "OpenID Connect (coming soon)",
                        "oauth2": "OAuth2 (coming soon)",
                    },
                    buttons_only=True,
                    default="api_key",
                    full_screen=True,
                )

                if auth_type_question.button == "api_key":
                    if auth_data.get("types") is None or "apiKey" not in auth_data["types"]:
                        addit_question = await self.ask_question(
                            "The API key authentication method is not supported by your backend. Do you want to continue?",
                            buttons_only=True,
                            buttons={"yes": "Yes", "no": "Go back"},
                        )
                        if addit_question.button != "yes":
                            continue

                    api_key = await self.ask_question(
                        "Enter your API key here. It will be saved in the .env file on the frontend.",
                        allow_empty=False,
                        verbose=True,
                    )
                    options["auth_type"] = "api_key"
                    options["api_key"] = api_key.text.strip()
                    break
                elif auth_type_question.button == "none":
                    options["auth_type"] = "none"
                    break
                else:
                    auth_type_question_trace = await self.ask_question(
                        "We are still working on getting this auth method implemented correctly. Can we contact you to get more info on how you would like it to work?",
                        allow_empty=False,
                        buttons={"yes": "Yes", "no": "No"},
                        default="yes",
                        buttons_only=True,
                    )
                    if auth_type_question_trace.button == "yes":
                        await telemetry.trace_code_event(
                            "swagger-auth-method",
                            {"type": auth_type_question.button},
                        )
                        await self.send_message("Thank you for submitting your request. We will be in touch.")
        else:
            options["auth_type"] = "login"

        auth_needed = await self.ask_question(
            "Do you need authentication in your app (login, register, etc.)?",
            buttons={
                "yes": "Yes",
                "no": "No",
            },
            buttons_only=True,
            default="no",
        )

        options["auth"] = auth_needed.button == "yes"
        options["jwt_secret"] = secrets.token_hex(32)
        options["refresh_token_secret"] = secrets.token_hex(32)

        self.next_state.knowledge_base["user_options"] = options
        self.state_manager.user_options = options

        if not self.state_manager.async_tasks:
            self.state_manager.async_tasks = []
            self.state_manager.async_tasks.append(asyncio.create_task(self.apply_template(options)))

        await self.ui.send_project_stage({"stage": ProjectStage.PROJECT_DESCRIPTION})

        description = await self.ask_question(
            "Please describe the app you want to build.",
            allow_empty=False,
            full_screen=True,
        )
        description = description.text.strip()
        self.state_manager.template["description"] = description

        await self.ui.send_project_description(
            {"project_description": description, "project_type": self.current_state.branch.project.project_type}
        )

        self.next_state.epics = [
            {
                "id": uuid4().hex,
                "name": "Build frontend",
                "source": "frontend",
                "description": description,
                "messages": [],
                "summary": None,
                "completed": False,
            }
        ]

        return True

    async def upload_docs(self, docs: str) -> bool:
        error = None
        url = urljoin(SWAGGER_EMBEDDINGS_API, "rag/upload")
        for attempt in range(3):
            log.debug(f"Uploading docs to RAG service... attempt {attempt}")
            try:
                async with httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(), timeout=httpx.Timeout(10.0, connect=5.0)
                ) as client:
                    resp = await client.post(
                        url,
                        json={
                            "text": docs,
                            "project_id": str(self.state_manager.project.id),
                        },
                        headers={"Authorization": f"Bearer {self.state_manager.get_access_token()}"},
                    )

                    if resp.status_code == 200:
                        log.debug("Uploading docs to RAG service successful")
                        return True
                    elif resp.status_code == 403:
                        log.debug("Uploading docs to RAG service failed, trying to refresh token")
                        access_token = await self.ui.send_token_expired()
                        self.state_manager.update_access_token(access_token)
                    else:
                        try:
                            error = resp.json()["error"]
                        except Exception as e:
                            capture_exception(e)
                            error = e
                        log.debug(f"Uploading docs to RAG service failed: {error}")

            except Exception as e:
                log.warning(f"Attempt {attempt + 1} failed: {e}", exc_info=True)
                capture_exception(e)

        await self.ui.send_message(
            f"An error occurred while uploading the docs. Error: {error if error else 'unknown'}",
        )
        return False

    async def apply_template(self, options: dict = {}):
        """
        Applies a template to the frontend.
        """
        if options["auth_type"] == "api_key" or options["auth_type"] == "none":
            template_name = "vite_react_swagger"
        else:
            template_name = "vite_react"
        template_class = PROJECT_TEMPLATES.get(template_name)
        if not template_class:
            log.error(f"Project template not found: {template_name}")
            return

        template = template_class(
            options,
            self.state_manager,
            self.process_manager,
        )
        self.state_manager.template["template"] = template
        log.info(f"Applying project template: {template.name}")
        summary = await template.apply()

        self.next_state.relevant_files = template.relevant_files
        self.next_state.modified_files = {}
        self.next_state.specification.template_summary = summary
