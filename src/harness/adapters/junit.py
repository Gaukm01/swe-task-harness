"""junit XML parsing.

The only source of test status. Nothing is ever scraped from stdout -- a test
whose own output prints the word "FAILED" would otherwise be enough to confuse
grading, and a solver can print whatever it likes.

pytest, jest (`jest-junit`), and most Go junit converters emit the same
`<testsuite><testcase>` shape, so this parser is shared rather than duplicated
per adapter.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

# A junit file produced by a normal test run is kilobytes. A file far larger
# than this is either a runaway suite or something crafted; either way, refusing
# to parse it beats spending the memory. (ElementTree resolves no external
# entities, but nested-entity expansion is still a denial-of-service shape.)
MAX_JUNIT_BYTES = 64 * 1024 * 1024

# Truncation limit for a failure message stored in the DB and rendered in a table.
MAX_MESSAGE_CHARS = 4000


class JunitParseError(Exception):
    """The junit file was absent, too large, or not parseable."""


@dataclass(frozen=True)
class JunitCase:
    """One `<testcase>` element, normalized."""

    classname: str
    name: str
    # pytest records the source file on every testcase, including the synthetic
    # one it emits for a collection failure. That attribute is the only reliable
    # link from "this module did not import" back to the selectors inside it.
    file: str
    duration_ms: int
    # One of: passed, failed, error, skipped
    result: str
    message: str | None = None
    # True when the element looks like a collection/import failure rather than a
    # test that ran and blew up.
    is_collection_error: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.classname, self.name)


def _text_of(element: ElementTree.Element) -> str:
    parts = [element.get("message") or "", (element.text or "").strip()]
    return "\n".join(part for part in parts if part).strip()


def _looks_like_collection_error(element: ElementTree.Element, case_name: str) -> bool:
    """Distinguish an import/collect failure from a test that errored in setup.

    pytest reports a collection failure as an `<error>` whose message mentions
    collecting, often on a synthetic testcase named after the file. Telling the
    two apart matters because a collection error usually means the solver broke
    the module outright -- a much more useful thing to report than "errored".
    """
    haystack = f"{element.get('message', '')} {element.get('type', '')} {case_name}".lower()
    return "collect" in haystack or "importerror" in haystack or ".py" in case_name


def parse_junit(xml_text: str) -> list[JunitCase]:
    """Parse junit XML into normalized cases."""
    if len(xml_text.encode("utf-8", errors="ignore")) > MAX_JUNIT_BYTES:
        raise JunitParseError(f"junit file exceeds {MAX_JUNIT_BYTES} bytes")
    if not xml_text.strip():
        raise JunitParseError("junit file is empty")

    try:
        root = ElementTree.fromstring(xml_text)  # noqa: S314 - no external entities in ET
    except ElementTree.ParseError as error:
        raise JunitParseError(f"malformed junit XML: {error}") from error

    cases: list[JunitCase] = []
    # `<testsuites>` wrapping `<testsuite>`, or a bare `<testsuite>`.
    for testcase in root.iter("testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        source_file = testcase.get("file", "")
        try:
            duration_ms = int(float(testcase.get("time", "0")) * 1000)
        except ValueError:
            duration_ms = 0

        result = "passed"
        message: str | None = None
        collection_error = False

        failure = testcase.find("failure")
        errored = testcase.find("error")
        skipped = testcase.find("skipped")

        if failure is not None:
            result = "failed"
            message = _text_of(failure)
        elif errored is not None:
            result = "error"
            message = _text_of(errored)
            collection_error = _looks_like_collection_error(errored, name)
        elif skipped is not None:
            result = "skipped"
            message = _text_of(skipped)

        cases.append(
            JunitCase(
                classname=classname,
                name=name,
                file=source_file,
                duration_ms=duration_ms,
                result=result,
                message=(message or None) and message[:MAX_MESSAGE_CHARS],
                is_collection_error=collection_error,
            )
        )
    return cases


def read_junit(path: Path) -> list[JunitCase]:
    """Read and parse a junit file from disk."""
    if not path.is_file():
        raise JunitParseError(f"no junit file at {path}")
    if path.stat().st_size > MAX_JUNIT_BYTES:
        raise JunitParseError(f"junit file exceeds {MAX_JUNIT_BYTES} bytes")
    return parse_junit(path.read_text(errors="replace"))
