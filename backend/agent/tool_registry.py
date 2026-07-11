"""Auto-extracted from multi_agent.py"""

import json, os, re
from datetime import datetime

class ToolRegistry:
    def __init__(self, tool_names):
        from backend.tools.tools import func_names, function_map, tools_definition as all_defs
        self.func_names = [n for n in tool_names if n in func_names]
        self.function_map = {n: function_map[n] for n in self.func_names if n in function_map}
        self._defs = [d for d in all_defs if d.get("function", {}).get("name") in self.func_names]
    def get_definitions(self):
        return self._defs
    def get_tool(self, name):
        return self.function_map.get(name)


