"""
A minimal OOP wrapper around dot/graphviz
"""
import difflib
from ..common_imports import *
from ..core.config import Config
import tempfile
import subprocess
import webbrowser
from typing import Literal

if Config.has_pil:
    from PIL import Image


class Color:
    def __init__(self, r: int, g: int, b: int, opacity: float = 1.0):
        self.r, self.g, self.b, self.opacity = r, g, b, opacity

    def __str__(self) -> str:
        opacity_int = int(self.opacity * 255)
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}{opacity_int:02x}"


SOLARIZED_LIGHT = {
    "base03": Color(0, 43, 54, 1),
    "base02": Color(7, 54, 66, 1),
    "base01": Color(88, 110, 117, 1),
    "base00": Color(101, 123, 131, 1),
    "base0": Color(131, 148, 150, 1),
    "base1": Color(147, 161, 161, 1),
    "base2": Color(238, 232, 213, 1),
    "base3": Color(253, 246, 227, 1),
    "yellow": Color(181, 137, 0, 1),
    "orange": Color(203, 75, 22, 1),
    "red": Color(220, 50, 47, 1),
    "magenta": Color(211, 54, 130, 1),
    "violet": Color(108, 113, 196, 1),
    "blue": Color(38, 139, 210, 1),
    "cyan": Color(42, 161, 152, 1),
    "green": Color(133, 153, 0, 1),
}


def _colorize(text: str, color: str) -> str:
    """
    Return `text` with ANSI color codes for `color` added.
    """
    colors = {
        "red": 31,
        "green": 32,
        "blue": 34,
        "yellow": 33,
        "magenta": 35,
        "cyan": 36,
        "white": 37,
    }
    return f"\033[{colors[color]}m{text}\033[0m"


def _get_colorized_diff(current: str, new: str) -> str:
    """
    Return a line-by-line colorized diff of the changes between `current` and
    `new`. each line removed from `current` is colored red, and each line added
    to `new` is colored green.
    """
    lines = []
    for line in difflib.unified_diff(
        current.splitlines(),
        new.splitlines(),
        n=2,  # number of lines of context around changes to show
        # fromfile="current", tofile="new"
        lineterm="",
    ):
        if line.startswith("@@") or line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("-"):
            lines.append(_colorize(line, "red"))
        elif line.startswith("+"):
            lines.append(_colorize(line, "green"))
        else:
            lines.append(line)
    return "\n".join(lines)


################################################################################
### tiny graphviz model
################################################################################
class Cell:
    def __init__(
        self,
        text: str,
        port: Optional[str] = None,
        colspan: int = 1,
        bgcolor: Color = SOLARIZED_LIGHT["base3"],
    ):
        self.port = port
        self.text = text
        self.colspan = colspan
        self.bgcolor = bgcolor

    def to_dot_string(self) -> str:
        return f'<TD PORT="{self.port}" BGCOLOR="{self.bgcolor}" COLSPAN="{self.colspan}">{self.text}</TD>'


class HTMLBuilder:
    def __init__(self):
        self.rows: List[List[Cell]] = []

    def add_row(self, cells: List[Cell]):
        self.rows.append(cells)

    def to_html_like_label(self) -> str:
        start = '<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0">'
        end = "</TABLE>"
        # broadcast colspan
        row_sizes = set([len(row) for row in self.rows])
        if len(row_sizes) > 1 and 1 not in row_sizes:
            raise ValueError(
                "All rows must have the same number of columns, or else only a single column"
            )
        num_columns = max(row_sizes)
        for row in self.rows:
            if len(row) == 1:
                row[0].colspan = num_columns
        rows = []
        for row in self.rows:
            rows.append("<TR>")
            for cell in row:
                rows.append(cell.to_dot_string())
            rows.append("</TR>")
        return start + "".join(rows) + end


class Node:
    def __init__(
        self,
        internal_name: str,
        label: str,
        color: Color = SOLARIZED_LIGHT["base3"],
        shape: str = "rect",
    ):
        """
        `shape` can be "rect", "record" or "Mrecord" for a record with rounded corners.
        """
        self.internal_name = internal_name
        self.label = label
        self.color = color
        self.shape = shape

    def to_dot_string(self) -> str:
        dot_label = f'"{self.label}"' if self.shape != "plain" else f"<{self.label}>"
        return f'"{self.internal_name}" [label={dot_label}, color="{self.color}", shape="{self.shape}"];'


class Edge:
    def __init__(
        self,
        source_node: Node,
        target_node: Node,
        source_port: Optional[str] = None,
        target_port: Optional[str] = None,
        arrowtail: Optional[str] = None,
        arrowhead: Optional[str] = None,
        label: str = "",
        color: Color = SOLARIZED_LIGHT["base03"],
    ):
        self.source_node = source_node
        self.target_node = target_node
        self.color = color
        self.label = label
        self.source_port = source_port
        self.target_port = target_port
        self.arrowtail = arrowtail
        self.arrowhead = arrowhead

    def to_dot_string(self) -> str:
        source = f'"{self.source_node.internal_name}"'
        target = f'"{self.target_node.internal_name}"'
        if self.source_port is not None:
            source += f":{self.source_port}"
        if self.target_port is not None:
            target += f":{self.target_port}"
        attrs = [f'label="{self.label}"', f'color="{self.color}"']
        if self.arrowtail is not None:
            attrs.append(f'arrowtail="{self.arrowtail}"')
        if self.arrowhead is not None:
            attrs.append(f'arrowhead="{self.arrowhead}"')
        return f"{source} -> {target} [{', '.join(attrs)}];"


class Group:
    def __init__(
        self,
        label: str,
        nodes: List[Node],
        parent: Optional["Group"] = None,
        border_color: Color = SOLARIZED_LIGHT["base03"],
    ):
        self.label = label
        self.nodes = nodes
        self.border_color = border_color
        self.parent = parent


FONT = "Helvetica"
FONT_SIZE = 10

GRAPH_CONFIG = {
    # "overlap": "scalexy",
    "overlap": "scale",
    "rankdir": "TB",  # top to bottom
    "fontname": FONT,
    "fontsize": FONT_SIZE,
    "fontcolor": SOLARIZED_LIGHT["base03"],
}

NODE_CONFIG = {
    "style": "rounded",
    "shape": "rect",
    "fontname": FONT,
    "fontsize": FONT_SIZE,
    "fontcolor": SOLARIZED_LIGHT["base03"],
}

EDGE_CONFIG = {
    "fontname": FONT,
    "fontsize": FONT_SIZE,
    "fontcolor": SOLARIZED_LIGHT["base03"],
}


def dict_to_dot_string(d: Dict[str, Any]) -> str:
    """Converts a dict to a dot string"""
    return ",".join([f'{k}="{v}"' for k, v in d.items()])


def _get_group_string_shallow(group: Group, children_string: str) -> str:
    nodes_string = " ".join([f'"{node.internal_name}"' for node in group.nodes])
    return f'subgraph "cluster_{group.label}" {{style="rounded"; label="{group.label}"; color="{group.border_color}"; {nodes_string};\n {children_string} }}'


def get_group_string(group: Group, groups_forest: Dict[Group, List[Group]]) -> str:
    children = groups_forest.get(group, [])
    return _get_group_string_shallow(
        group,
        children_string="\n".join(
            [get_group_string(child, groups_forest=groups_forest) for child in children]
        ),
    )


def to_dot_string(
    nodes: List[Node],
    edges: List[Edge],
    groups: List[Group],
    rankdir: Literal["TB", "BT", "LR", "RL"] = "TB",
) -> str:
    """Converts a graph to a dot string"""
    joiner = "\n" + " " * 12
    ### global config
    graph_config = copy.deepcopy(GRAPH_CONFIG)
    graph_config["rankdir"] = rankdir
    graph_config = f"graph [ {dict_to_dot_string(graph_config)} ];"
    node_config = f"node [ {dict_to_dot_string(NODE_CONFIG)} ];"
    edge_config = f"edge [ {dict_to_dot_string(EDGE_CONFIG)} ];"
    preamble = joiner.join([graph_config, node_config, edge_config])
    ### nodes
    node_strings = []
    for node in nodes:
        node_strings.append(node.to_dot_string())
    nodes_part = joiner.join(node_strings)
    ### edges
    edge_strings = []
    for edge in edges:
        edge_strings.append(edge.to_dot_string())
    edges_part = joiner.join(edge_strings)
    ### groups
    groups_forest = {
        group: [g for g in groups if g.parent is group] for group in groups
    }
    roots = [group for group in groups if group.parent is None]
    group_strings = []
    for group in roots:
        group_strings.append(get_group_string(group, groups_forest=groups_forest))
    groups_part = joiner.join(group_strings)
    result = f"""
    digraph G {{
        {preamble}
        {nodes_part}
        {edges_part}
        {groups_part}
        }}
    """
    return result


def write_output(
    dot_string: str,
    output_ext: str,
    output_path: Optional[Path] = None,
    show_how: Literal["none", "browser", "inline", "open"] = "none",
):
    # make a temp file and write the dot string to it
    if output_path is None:
        tfile = tempfile.NamedTemporaryFile(suffix=f".{output_ext}", delete=False)
        output_path = Path(tfile.name)
    with tempfile.NamedTemporaryFile(mode="w", delete=True) as f:
        path = f.name
        with open(path, "w") as f:
            f.write(dot_string)
        cmd = f"dot -T{output_ext} -o{output_path} {path}"
        subprocess.call(cmd, shell=True)
    if show_how == "browser":
        assert output_ext in [
            "png",
            "jpg",
            "jpeg",
            "svg",
        ], "Can only show png, jpg, jpeg, or svg in browser"
        webbrowser.open(str(output_path))
        return
    if show_how == "inline" or show_how == "open":
        assert (
            Config.has_pil
        ), "Pillow is not installed. Please install it to show images inline"
        img = Image.open(output_path, "r")
        if show_how == "inline":
            return img
        else:
            img.show()
