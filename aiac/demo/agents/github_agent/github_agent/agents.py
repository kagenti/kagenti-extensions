from crewai import Agent, Crew, Process, Task
from github_agent.config import Settings
from github_agent.llm import CrewLLM
from github_agent.prompts import TOOL_CALL_PROMPT, INFO_PARSER_PROMPT


class GithubAgents:
    def __init__(self, config: Settings, github_tools):
        self.llm = CrewLLM(config)

        ###################
        # Pre-requisite extractor (no tools)
        ###################
        self.prereq_identifier = Agent(
            role="Pre-requisite Extractor",
            goal="Extract information about GitHub artifacts from the user's query",
            backstory=INFO_PARSER_PROMPT,
            verbose=True,
            llm=self.llm.llm,
        )

        self.prereq_identifier_task = Task(
            description="User query: {request}",
            agent=self.prereq_identifier,
            expected_output=(
                'A JSON object with keys "owner", "repo", "ref", "path", "numbers". '
                'Example: {"owner": "kagenti", "repo": "kagenti", "ref": null, "path": null, "numbers": null}'
            ),
        )

        self.prereq_id_crew = Crew(
            agents=[self.prereq_identifier],
            tasks=[self.prereq_identifier_task],
            process=Process.sequential,
            verbose=True,
        )

        ###################
        # GitHub operations researcher
        ###################
        self.github_researcher = Agent(
            role="GitHub Operations Analyst",
            goal=(
                "Answer the user's query using MCP tools. "
                "Prefer read-only operations. Be explicit about repo owner/name and filters."
            ),
            backstory=TOOL_CALL_PROMPT,
            tools=github_tools,
            verbose=True,
            llm=self.llm.llm,
            inject_date=True,
            max_iter=6,
            max_retry_limit=3,
            respect_context_window=True,
        )

        self.github_query_task = Task(
            description=(
                "Use GitHub MCP tools to answer the user's query.\n"
                "User query: {request}\n"
                "Repository owner: {owner}\n"
                "Repository name: {repo}\n"
                "Branch/tag/sha: {ref}\n"
                "File path: {path}\n"
                "Issue/PR numbers: {numbers}"
            ),
            agent=self.github_researcher,
            expected_output=(
                "A well formatted, detailed report directly answering the user's query, "
                "citing the tool output to support the answer."
            ),
        )

        self.crew = Crew(
            agents=[self.github_researcher],
            tasks=[self.github_query_task],
            process=Process.sequential,
            verbose=True,
        )
