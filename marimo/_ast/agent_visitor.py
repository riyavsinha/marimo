import ast


class AgentVisitor(ast.NodeVisitor):
    """Find any agent runs in the AST.
    This should be a call to mo.ai.run_agent.
    """

    def __init__(self) -> None:
        super().__init__()
        # For now only track the first agent name
        self.has_agent_run: bool = False
        self._agent_name: str | None = None

    def visit_Call(self, node: ast.Call) -> None:
        # Check if the call is mo.ai.run_agent
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "run_agent"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "agents"
            and isinstance(node.func.value.value, ast.Attribute)
            and node.func.value.value.attr == "ai"
            and isinstance(node.func.value.value.value, ast.Name)
            and node.func.value.value.value.id == "mo"
        ):
            self.has_agent_run = True
            # Look for the name parameter in keywords
            name = None
            found_keyword = False
            for keyword in node.keywords:
                if keyword.arg == "name":
                    if isinstance(keyword.value, ast.Constant):
                        name = keyword.value.value
                        found_keyword = True
                    break

            # Also check if there is a second argument
            if not found_keyword:
                if len(node.args) > 1:
                    if isinstance(node.args[1], ast.Constant):
                        name = node.args[1].value

            self._agent_name = name

        # Continue walking through the AST
        self.generic_visit(node)

    def get_agent_name(self) -> str | None:
        """Return the first agent name found."""
        return self._agent_name
