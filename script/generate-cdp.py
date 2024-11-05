#!/usr/bin/env python

# initially copied from the "python-chrome-devtools-protocol" project:
# https://raw.githubusercontent.com/HyperionGray/python-chrome-devtools-protocol/5463a5f3d201/generator/generate.py
# https://raw.githubusercontent.com/HyperionGray/python-chrome-devtools-protocol/5463a5f3d201/cdp/util.py
#
# The MIT License (MIT)
#
# Copyright (c) 2018 Hyperion Gray
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import annotations

import argparse
import builtins
import itertools
import logging
import operator
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from textwrap import dedent, indent as tw_indent
from typing import cast

import inflection  # type: ignore[import]
import requests


URL_API_NPMJS_LATEST = "https://registry.npmjs.org/devtools-protocol/latest"
JSON_PROTOCOL_URLS = [
    "https://github.com/ChromeDevTools/devtools-protocol/raw/{ref}/json/browser_protocol.json",
    "https://github.com/ChromeDevTools/devtools-protocol/raw/{ref}/json/js_protocol.json",
]
OUTPUT_PATH = Path(__file__).parent.parent / "src"

DOMAINS_REQUIRED = ["target", "inspector"]


parser = argparse.ArgumentParser()
parser.add_argument("domains", nargs="*")
parser.add_argument("--ref")
parser.add_argument("-p", "--package", default="streamlink.webbrowser.cdp.devtools")
parser.add_argument("-l", "--loglevel", choices=["debug", "info", "warning", "error"], default="info")


logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler(stream=sys.stdout))


# ----


SHARED_HEADER = """# DO NOT EDIT THIS FILE!
#
# This file is generated from the CDP specification. If you need to make
# changes, edit the generator and regenerate all modules.
#
# CDP version: {ref}"""

INIT_HEADER = f"{SHARED_HEADER}\n\n"

MODULE_HEADER = f"""{SHARED_HEADER}
# CDP domain: {{domain}}{{experimental}}

from __future__ import annotations

import enum
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

{{imports}}


"""

UTIL = f"""{SHARED_HEADER}

from __future__ import annotations

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from typing_extensions import TypeAlias


T_JSON_DICT: TypeAlias = "dict[str, Any]"
_event_parsers = {{{{}}}}


def event_class(method):
    \"\"\"A decorator that registers a class as an event class.\"\"\"

    def decorate(cls):
        _event_parsers[method] = cls
        return cls

    return decorate


def parse_json_event(json: T_JSON_DICT) -> Any:
    \"\"\"Parse a JSON dictionary into a CDP event.\"\"\"
    return _event_parsers[json["method"]].from_json(json["params"])
"""


def indent(s: str, n: int):
    """A shortcut for ``textwrap.indent`` that always uses spaces."""
    return tw_indent(s, n * " ")


BACKTICK_RE = re.compile(r"`([^`]+)`(\w+)?")


def escape_backticks(docstr: str) -> str:
    """
    Escape backticks in a docstring by doubling them up.

    This is a little tricky because RST requires a non-letter character after
    the closing backticks, but some CDPs docs have things like "`AxNodeId`s".
    If we double the backticks in that string, then it won't be valid RST. The
    fix is to insert an apostrophe if an "s" trails the backticks.
    """

    def replace_one(match):
        if match.group(2) == "s":
            return f"``{match.group(1)}``'s"
        elif match.group(2):
            # This case (some trailer other than "s") doesn't currently exist
            # in the CDP definitions, but it's here just to be safe.
            return f"``{match.group(1)}`` {match.group(2)}"
        else:
            return f"``{match.group(1)}``"

    # If there's an odd number of backticks (erroneous description), remove all backticks :(
    if docstr.count("`") % 2:
        docstr = docstr.replace("`", "")

    # Sometimes pipes are used where backticks should have been used.
    docstr = docstr.replace("|", "`")
    return BACKTICK_RE.sub(replace_one, docstr)


def inline_doc(description) -> str:
    """Generate an inline doc, e.g. ``#: This type is a ...``"""
    if not description:
        return ""

    description = escape_backticks(description)
    lines = [f"#: {line}".rstrip() for line in description.split("\n")]
    return "\n".join(lines)


def docstring(description: str | None) -> str:
    """Generate a docstring from a description."""
    if not description:
        return ""

    description = escape_backticks(description)
    return dedent(f'"""\n{description}\n"""')


def is_builtin(name: str) -> bool:
    """Return True if ``name`` would shadow a builtin."""
    try:
        getattr(builtins, name)
        return True
    except AttributeError:
        return False


def snake_case(name: str) -> str:
    """
    Convert a camel case name to snake case. If the name would shadow a
    Python builtin, then append an underscore.
    """
    name = inflection.underscore(name)
    if is_builtin(name):
        name += "_"
    return name


def ref_to_python(ref: str, domain: str) -> str:
    """
    Convert a CDP ``$ref`` to the name of a Python type.

    For a dotted ref, the part before the dot is snake cased.
    """
    if "." not in ref:
        return f"{ref}"

    _domain, subtype = ref.split(".")
    if _domain == domain:
        return subtype

    return f"{snake_case(_domain)}.{subtype}"


class CdpPrimitiveType(Enum):
    """All of the CDP types that map directly to a Python type."""

    boolean = "bool"
    integer = "int"
    number = "float"
    object = "dict"
    string = "str"

    @classmethod
    def get_annotation(cls, cdp_type):
        """Return a type annotation for the CDP type."""
        if cdp_type == "any":
            return "Any"
        else:
            return cls[cdp_type].value

    @classmethod
    def get_constructor(cls, cdp_type, val):
        """Return the code to construct a value for a given CDP type."""
        if cdp_type == "any":
            return val
        else:
            cons = cls[cdp_type].value
            return f"{cons}({val})"


@dataclass
class CdpItems:
    """Represents the type of a repeated item."""

    type: str
    ref: str

    @classmethod
    def from_json(cls, type_) -> "CdpItems":
        """Generate code to instantiate an item from a JSON object."""
        return cls(type_.get("type"), type_.get("$ref"))


@dataclass
class CdpProperty:
    """A property belonging to a non-primitive CDP type."""

    name: str
    description: str | None
    type: str | None
    ref: str | None
    enum: list[str]
    items: CdpItems | None
    optional: bool
    experimental: bool
    deprecated: bool
    domain: str

    @property
    def py_name(self) -> str:
        """Get this property's Python name."""
        return snake_case(self.name)

    @property
    def py_annotation(self) -> str:
        """This property's Python type annotation."""
        if self.items:
            if self.items.ref:
                py_ref = ref_to_python(self.items.ref, self.domain)
                ann = f"list[{py_ref}]"
            else:
                ann = f"list[{CdpPrimitiveType.get_annotation(self.items.type)}]"
        else:
            if self.ref:
                py_ref = ref_to_python(self.ref, self.domain)
                ann = py_ref
            else:
                ann = CdpPrimitiveType.get_annotation(cast(str, self.type))
        if self.optional:
            ann = f"{ann} | None"
        return ann

    @classmethod
    def from_json(cls, property_, domain) -> "CdpProperty":
        """Instantiate a CDP property from a JSON object."""
        return cls(
            property_["name"],
            property_.get("description"),
            property_.get("type"),
            property_.get("$ref"),
            property_.get("enum"),
            CdpItems.from_json(property_["items"]) if "items" in property_ else None,
            property_.get("optional", False),
            property_.get("experimental", False),
            property_.get("deprecated", False),
            domain,
        )

    def generate_decl(self) -> str:
        """Generate the code that declares this property."""
        code = inline_doc(self.description)
        if code:
            code += "\n"
        code += f"{self.py_name}: {self.py_annotation}"
        if self.optional:
            code += " = None"
        return code

    def generate_to_json(self, dict_: str, use_self: bool = True) -> str:
        """Generate the code that exports this property to the specified JSON dict."""
        self_ref = "self." if use_self else ""
        assign = f'{dict_}["{self.name}"] = '
        if self.items:
            if self.items.ref:
                assign += f"[i.to_json() for i in {self_ref}{self.py_name}]"
            else:
                assign += f"list({self_ref}{self.py_name})"
        else:
            if self.ref:
                assign += f"{self_ref}{self.py_name}.to_json()"
            else:
                assign += f"{self_ref}{self.py_name}"
        if self.optional:
            code = dedent(f"""\
                if {self_ref}{self.py_name} is not None:
                    {assign}""")
        else:
            code = assign
        return code

    def generate_from_json(self, dict_) -> str:
        """Generate the code that creates an instance from a JSON dict named ``dict_``."""
        if self.items:
            if self.items.ref:
                py_ref = ref_to_python(self.items.ref, self.domain)
                expr = f'[{py_ref}.from_json(i) for i in {dict_}["{self.name}"]]'
            else:
                cons = CdpPrimitiveType.get_constructor(self.items.type, "i")
                if cons == "i":
                    expr = f'list({dict_}["{self.name}"])'
                else:
                    expr = f'[{cons} for i in {dict_}["{self.name}"]]'
        else:
            if self.ref:
                py_ref = ref_to_python(self.ref, self.domain)
                expr = f'{py_ref}.from_json({dict_}["{self.name}"])'
            else:
                expr = CdpPrimitiveType.get_constructor(self.type, f'{dict_}["{self.name}"]')
        if self.optional:
            expr = f'{expr} if "{self.name}" in {dict_} else None'
        return expr


@dataclass
class CdpType:
    """A top-level CDP type."""

    id: str
    description: str | None
    type: str
    items: CdpItems | None
    enum: list[str]
    properties: list[CdpProperty]
    domain: str

    @classmethod
    def from_json(cls, type_, domain) -> "CdpType":
        """Instantiate a CDP type from a JSON object."""
        return cls(
            type_["id"],
            type_.get("description"),
            type_["type"],
            CdpItems.from_json(type_["items"]) if "items" in type_ else None,
            type_.get("enum"),
            [CdpProperty.from_json(p, domain) for p in type_.get("properties", [])],
            domain,
        )

    def generate_code(self) -> str:
        """Generate Python code for this type."""
        logger.debug(f"Generating type {self.id}: {self.type}")
        if self.enum:
            return self.generate_enum_code()
        elif self.properties:
            return self.generate_class_code()
        else:
            return self.generate_primitive_code()

    def generate_primitive_code(self) -> str:
        """Generate code for a primitive type."""
        if self.items:
            if self.items.ref:
                nested_type = ref_to_python(self.items.ref, self.domain)
            else:
                nested_type = CdpPrimitiveType.get_annotation(self.items.type)
            py_type = f"list[{nested_type}]"
            superclass = "list"
        else:
            # A primitive type cannot have a ref, so there is no branch here.
            py_type = CdpPrimitiveType.get_annotation(self.type)
            superclass = py_type

        code = f"class {self.id}({superclass}):\n"
        doc = docstring(self.description)
        if doc:
            code += indent(doc, 4) + "\n"

        def_to_json = dedent(f"""\
            def to_json(self) -> {py_type}:
                return self""")
        code += indent(def_to_json, 4)

        def_from_json = dedent(f"""\
            @classmethod
            def from_json(cls, json: {py_type}) -> {self.id}:
                return cls(json)""")
        code += "\n\n" + indent(def_from_json, 4)

        def_repr = dedent(f"""\
            def __repr__(self):
                return f\"{self.id}({{super().__repr__()}})\"""")
        code += "\n\n" + indent(def_repr, 4)

        return code

    def generate_enum_code(self) -> str:
        """
        Generate an "enum" type.

        Enums are handled by making a python class that contains only class
        members. Each class member is upper snaked case, e.g.
        ``MyTypeClass.MY_ENUM_VALUE`` and is assigned a string value from the
        CDP metadata.
        """
        def_to_json = dedent("""\
            def to_json(self) -> str:
                return self.value""")

        def_from_json = dedent(f"""\
            @classmethod
            def from_json(cls, json: str) -> {self.id}:
                return cls(json)""")

        code = f"class {self.id}(enum.Enum):\n"
        doc = docstring(self.description)
        if doc:
            code += indent(doc, 4) + "\n"
        for enum_member in self.enum:
            snake_name = snake_case(enum_member).upper()
            enum_code = f'{snake_name} = "{enum_member}"\n'
            code += indent(enum_code, 4)
        code += "\n" + indent(def_to_json, 4)
        code += "\n\n" + indent(def_from_json, 4)

        return code

    def generate_class_code(self) -> str:
        """
        Generate a class type.

        Top-level types that are defined as a CDP ``object`` are turned into Python
        dataclasses.
        """
        # children = set()
        code = dedent(f"""\
            @dataclass
            class {self.id}:\n""")
        doc = docstring(self.description)
        if doc:
            code += indent(doc, 4) + "\n"

        # Emit property declarations. These are sorted so that optional
        # properties come after required properties, which is required to make
        # the dataclass constructor work.
        props = list(self.properties)
        props.sort(key=operator.attrgetter("optional"))
        code += "\n\n".join(indent(p.generate_decl(), 4) for p in props)
        code += "\n\n"

        # Emit to_json() method. The properties are sorted in the same order as
        # above for readability.
        def_to_json = dedent("""\
            def to_json(self) -> T_JSON_DICT:
                json: T_JSON_DICT = {}
        """)
        assigns = (p.generate_to_json(dict_="json") for p in props)
        def_to_json += indent("\n".join(assigns), 4)
        def_to_json += "\n"
        def_to_json += indent("return json", 4)
        code += indent(def_to_json, 4) + "\n\n"

        # Emit from_json() method. The properties are sorted in the same order
        # as above for readability.
        def_from_json = dedent(f"""\
            @classmethod
            def from_json(cls, json: T_JSON_DICT) -> {self.id}:
                return cls(
        """)
        from_jsons = []
        for p in props:
            from_json = p.generate_from_json(dict_="json")
            from_jsons.append(f"{p.py_name}={from_json},")
        def_from_json += indent("\n".join(from_jsons), 8)
        def_from_json += "\n"
        def_from_json += indent(")", 4)
        code += indent(def_from_json, 4)

        return code

    def get_refs(self):
        """Return all refs for this type."""
        refs = set()
        if self.enum:
            # Enum types don't have refs.
            pass
        elif self.properties:
            # Enumerate refs for a class type.
            for prop in self.properties:
                if prop.items and prop.items.ref:
                    refs.add(prop.items.ref)
                elif prop.ref:
                    refs.add(prop.ref)
        else:
            # A primitive type can't have a direct ref, but it can have an items
            # which contains a ref.
            if self.items and self.items.ref:
                refs.add(self.items.ref)
        return refs


class CdpParameter(CdpProperty):
    """A parameter to a CDP command."""

    def generate_code(self) -> str:
        """Generate the code for a parameter in a function call."""
        if self.items:
            if self.items.ref:
                nested_type = ref_to_python(self.items.ref, self.domain)
                py_type = f"list[{nested_type}]"
            else:
                nested_type = CdpPrimitiveType.get_annotation(self.items.type)
                py_type = f"list[{nested_type}]"
        else:
            if self.ref:
                py_type = f"{ref_to_python(self.ref, self.domain)}"
            else:
                py_type = CdpPrimitiveType.get_annotation(cast(str, self.type))
        if self.optional:
            py_type = f"{py_type} | None"
        code = f"{self.py_name}: {py_type}"
        if self.optional:
            code += " = None"
        return code

    def generate_decl(self) -> str:
        """Generate the declaration for this parameter."""
        if self.description:
            code = inline_doc(self.description)
            code += "\n"
        else:
            code = ""
        code += f"{self.py_name}: {self.py_annotation}"
        return code

    def generate_doc(self) -> str:
        """Generate the docstring for this parameter."""
        doc = f":param {self.py_name}:"

        if self.experimental:
            doc += " **(EXPERIMENTAL)**"

        if self.optional:
            doc += " *(Optional)*"

        if self.description:
            desc = self.description.replace("`", "``").replace("\n", " ")
            doc += f" {desc}"
        return doc

    def generate_from_json(self, dict_) -> str:
        """Generate the code to instantiate this parameter from a JSON dict."""
        code = super().generate_from_json(dict_)
        return f"{self.py_name}={code}"


class CdpReturn(CdpProperty):
    """A return value from a CDP command."""

    @property
    def py_annotation(self):
        """Return the Python type annotation for this return."""
        if self.items:
            if self.items.ref:
                py_ref = ref_to_python(self.items.ref, self.domain)
                ann = f"list[{py_ref}]"
            else:
                py_type = CdpPrimitiveType.get_annotation(self.items.type)
                ann = f"list[{py_type}]"
        else:
            if self.ref:
                py_ref = ref_to_python(self.ref, self.domain)
                ann = f"{py_ref}"
            else:
                ann = CdpPrimitiveType.get_annotation(self.type)
        if self.optional:
            ann = f"{ann} | None"
        return ann

    def generate_doc(self):
        """Generate the docstring for this return."""
        if self.description:
            doc = self.description.replace("\n", " ")
            if self.optional:
                doc = f"*(Optional)* {doc}"
        else:
            doc = ""
        return doc

    def generate_return(self, dict_):
        """Generate code for returning this value."""
        return super().generate_from_json(dict_)


@dataclass
class CdpCommand:
    """A CDP command."""

    name: str
    description: str
    experimental: bool
    deprecated: bool
    parameters: list[CdpParameter]
    returns: list[CdpReturn]
    domain: str

    @property
    def py_name(self):
        """Get a Python name for this command."""
        return snake_case(self.name)

    @classmethod
    def from_json(cls, command, domain) -> "CdpCommand":
        """Instantiate a CDP command from a JSON object."""
        parameters = command.get("parameters", [])
        returns = command.get("returns", [])

        return cls(
            command["name"],
            command.get("description"),
            command.get("experimental", False),
            command.get("deprecated", False),
            [cast(CdpParameter, CdpParameter.from_json(p, domain)) for p in parameters],
            [cast(CdpReturn, CdpReturn.from_json(r, domain)) for r in returns],
            domain,
        )

    def generate_code(self) -> str:
        """Generate code for a CDP command."""
        # Generate the function header
        if len(self.returns) == 0:
            ret_type = "None"
        elif len(self.returns) == 1:
            ret_type = self.returns[0].py_annotation
        else:
            nested_types = ", ".join(r.py_annotation for r in self.returns)
            ret_type = f"tuple[{nested_types}]"
        ret_type = f"Generator[T_JSON_DICT, T_JSON_DICT, {ret_type}]"

        code = ""

        code += f"def {self.py_name}("
        ret = f") -> {ret_type}:\n"

        parameters = sorted(self.parameters, key=operator.attrgetter("optional"))

        if parameters:
            # FIX order of parameters: optional parameters MUST come last
            params = [f"{p.generate_code()}," for p in parameters]
            code += "\n"
            code += indent("\n".join(params), 4)
            code += "\n"
            code += ret
        else:
            code += ret

        # Generate the docstring
        doc = ""
        if self.description:
            doc = self.description
        if self.experimental:
            doc += "\n\n**EXPERIMENTAL**"
        if parameters and doc:
            doc += "\n\n"
        elif not parameters and self.returns:
            doc += "\n"
        doc += "\n".join(p.generate_doc() for p in parameters)
        if len(self.returns) == 1:
            doc += "\n"
            ret_doc = self.returns[0].generate_doc()
            doc += f":returns: {ret_doc}".rstrip()
        elif len(self.returns) > 1:
            doc += "\n"
            doc += ":returns: A tuple with the following items:\n\n"
            ret_docs = "\n".join(f"{i}. **{r.name}** - {r.generate_doc()}".rstrip() for i, r in enumerate(self.returns))
            doc += indent(ret_docs, 4)
        if doc:
            code += indent(docstring(doc), 4)

        # Generate the function body
        if parameters:
            code += "\n"
            code += indent("params: T_JSON_DICT = {}", 4)
            code += "\n"
        assigns = (p.generate_to_json(dict_="params", use_self=False) for p in parameters)
        code += indent("\n".join(assigns), 4)
        code += "\n"
        code += indent("cmd_dict: T_JSON_DICT = {\n", 4)
        code += indent(f'"method": "{self.domain}.{self.name}",\n', 8)
        if parameters:
            code += indent('"params": params,\n', 8)
        code += indent("}\n", 4)
        code += indent(f"{'json = ' if len(self.returns) else ''}yield cmd_dict", 4)
        if len(self.returns) == 0:
            pass
        elif len(self.returns) == 1:
            ret = self.returns[0].generate_return(dict_="json")
            code += indent(f"\nreturn {ret}", 4)
        else:
            ret = "\nreturn (\n"
            expr = "\n".join(f"{r.generate_return(dict_='json')}," for r in self.returns)
            ret += indent(expr, 4)
            ret += "\n)"
            code += indent(ret, 4)
        return code

    def get_refs(self):
        """Get all refs for this command."""
        refs = set()
        for type_ in itertools.chain(self.parameters, self.returns):
            if type_.items and type_.items.ref:
                refs.add(type_.items.ref)
            elif type_.ref:
                refs.add(type_.ref)
        return refs


@dataclass
class CdpEvent:
    """A CDP event object."""

    name: str
    description: str | None
    deprecated: bool
    experimental: bool
    parameters: list[CdpParameter]
    domain: str

    @property
    def py_name(self):
        """Return the Python class name for this event."""
        return inflection.camelize(self.name, uppercase_first_letter=True)

    @classmethod
    def from_json(cls, json_: dict, domain: str):
        """Create a new CDP event instance from a JSON dict."""
        return cls(
            json_["name"],
            json_.get("description"),
            json_.get("deprecated", False),
            json_.get("experimental", False),
            [cast(CdpParameter, CdpParameter.from_json(p, domain)) for p in json_.get("parameters", [])],
            domain,
        )

    def generate_code(self) -> str:
        """Generate code for a CDP event."""
        code = dedent(f"""\
            @event_class(\"{self.domain}.{self.name}\")
            @dataclass
            class {self.py_name}:""")

        code += "\n"
        desc = ""
        if self.description or self.experimental:
            if self.experimental:
                desc += "**EXPERIMENTAL**\n\n"

            if self.description:
                desc += self.description

            code += indent(docstring(desc), 4)
            code += "\n"
        code += indent("\n".join(p.generate_decl() for p in self.parameters), 4)
        code += "\n\n"
        def_from_json = dedent(f"""\
            @classmethod
            def from_json(cls, json: T_JSON_DICT) -> {self.py_name}:
                return cls(
        """)
        code += indent(def_from_json, 4)
        from_json = "\n".join(f"{p.generate_from_json(dict_='json')}," for p in self.parameters)
        code += indent(from_json, 12)
        code += "\n"
        code += indent(")", 8)
        return code

    def get_refs(self):
        """Get all refs for this event."""
        refs = set()
        for param in self.parameters:
            if param.items and param.items.ref:
                refs.add(param.items.ref)
            elif param.ref:
                refs.add(param.ref)
        return refs


@dataclass
class CdpDomain:
    """A CDP domain contains metadata, types, commands, and events."""

    domain: str
    description: str | None
    experimental: bool
    dependencies: list[str]
    types: list[CdpType]
    commands: list[CdpCommand]
    events: list[CdpEvent]

    @property
    def module(self):
        """The name of the Python module for this CDP domain."""
        return snake_case(self.domain)

    @classmethod
    def from_json(cls, domain: dict):
        """Instantiate a CDP domain from a JSON object."""
        types = domain.get("types", [])
        commands = domain.get("commands", [])
        events = domain.get("events", [])
        domain_name = domain["domain"]

        return cls(
            domain_name,
            domain.get("description"),
            domain.get("experimental", False),
            domain.get("dependencies", []),
            [CdpType.from_json(_type, domain_name) for _type in types],
            [CdpCommand.from_json(command, domain_name) for command in commands],
            [CdpEvent.from_json(event, domain_name) for event in events],
        )

    def generate_code(self, ref: str, package: str) -> str:
        """Generate the Python module code for a given CDP domain."""
        exp = " (experimental)" if self.experimental else ""
        imports = self.generate_imports(package)
        code = MODULE_HEADER.format(
            ref=ref,
            domain=self.domain,
            experimental=exp,
            imports=imports,
        )
        item_iter: Iterator[CdpEvent | CdpCommand | CdpType] = itertools.chain(
            iter(self.types),
            iter(self.commands),
            iter(self.events),
        )
        code += "\n\n\n".join(item.generate_code() for item in item_iter)
        code += "\n"
        return code

    def get_imports(self):
        refs = set()
        for type_ in self.types:
            refs |= type_.get_refs()
        for command in self.commands:
            refs |= command.get_refs()
        for event in self.events:
            refs |= event.get_refs()
        dependencies = set()
        for ref in refs:
            try:
                domain, _ = ref.split(".")
            except ValueError:
                continue
            if domain != self.domain:
                dependencies.add(snake_case(domain))

        return dependencies

    def generate_imports(self, package: str):
        """
        Determine which modules this module depends on and emit the code to
        import those modules.

        Notice that CDP defines a ``dependencies`` field for each domain, but
        these dependencies are a subset of the modules that we actually need to
        import to make our Python code work correctly and type safe. So we
        ignore the CDP's declared dependencies and compute them ourselves.
        """
        dependencies = self.get_imports()
        imports = [f"import {package}.{d} as {d}\n" for d in sorted(dependencies)]
        imports.append(f"from {package}.util import T_JSON_DICT, event_class")

        return "".join(imports)


def parse(schema: dict) -> list[CdpDomain]:
    """Parse JSON protocol description and return domain objects."""
    version = schema["version"]
    assert (version["major"], version["minor"]) == ("1", "3")
    domains = []
    for domain in schema["domains"]:
        domains.append(CdpDomain.from_json(domain))
    return domains


def generate_init(init_path: Path, ref: str, package: str, domains: list[CdpDomain]):
    """Generate an ``__init__.py`` that exports the specified modules."""
    with init_path.open("w") as init_file:
        init_file.write(INIT_HEADER.format(ref=ref))
        for module in sorted([domain.module for domain in domains] + ["util"]):
            init_file.write(f"import {package}.{module} as {module}\n")


def generate_util(util_path: Path, ref: str):
    """Generate a ``util.py`` that is imported by the domain module files."""
    with util_path.open("w") as util_file:
        util_file.write(UTIL.format(ref=ref))


def main():
    """Main entry point."""
    args = parser.parse_args()

    logger.setLevel(args.loglevel.upper())

    output_path = OUTPUT_PATH / Path(*args.package.split("."))
    logger.info(f"Output: {output_path}")

    session = requests.Session()
    session.headers["User-Agent"] = "streamlink/streamlink"

    ref = args.ref
    if not ref:
        logger.info("Fetching latest tag from NPMJS")
        try:
            npmjs_api_data = session.get(URL_API_NPMJS_LATEST).json()
            ref = f"v{npmjs_api_data['version']}"
        except (requests.HTTPError, KeyError) as err:
            logger.exception(err)
            return
        logger.info(f"Latest tag: {ref}")

    json_data = []
    try:
        for url in JSON_PROTOCOL_URLS:
            url = url.format(ref=ref)
            logger.info(f"Fetching {url}")
            res = session.get(url, timeout=10)
            logger.debug("Parsing JSON...")
            json_data.append(res.json())
    except requests.HTTPError as err:
        logger.exception(err)
        return

    # Parse domains
    logger.info("Parsing domains...")
    domain_data = []
    for data in json_data:
        domain_data.extend(parse(data))
    domain_map = {snake_case(domain.domain): domain for domain in domain_data}

    selected_domains = {snake_case(name) for name in args.domains + DOMAINS_REQUIRED}
    for name in selected_domains:
        if name not in domain_map:
            logger.error(f"Invalid domain: {name}")
            return

    # Calculate which domains are required
    required_domains = set()

    def add_required_domains(current):
        if current in required_domains:
            return
        required_domains.add(current)
        deps = domain_map[current].get_imports()
        for dep in deps:
            add_required_domains(dep)

    for selection in selected_domains:
        add_required_domains(selection)

    domains = [domain_map[name] for name in sorted(required_domains)]

    logger.info("Writing output...")
    output_path.mkdir(parents=True, exist_ok=True)

    # Remove generated code
    for subpath in output_path.iterdir():
        if subpath.is_file():
            subpath.unlink()

    for domain in domains:
        logger.info(f"Generating module: {domain.domain} -> {domain.module}.py")
        module_path = output_path / f"{domain.module}.py"
        with module_path.open("w") as module_file:
            module_file.write(domain.generate_code(ref, args.package))

    init_path = output_path / "__init__.py"
    util_path = output_path / "util.py"
    generate_init(init_path, ref, args.package, domains)
    generate_util(util_path, ref)


if __name__ == "__main__":
    main()
