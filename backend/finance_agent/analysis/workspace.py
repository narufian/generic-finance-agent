"""E2B-backed workspace used by the analysis tools."""

from __future__ import annotations

import asyncio
import json
import posixpath
import shlex
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

from e2b import Sandbox

from .models import ApprovedSource, ExecutionArtifact


SOURCE_ROOT = PurePosixPath("/home/user/work/sources")
SCRATCH_ROOT = PurePosixPath("/home/user/work/scratch")


class AnalysisWorkspace(Protocol):
    async def seed_sources(self, sources: list[ApprovedSource]) -> None: ...
    async def glob(self, pattern: str) -> list[str]: ...
    async def grep(self, pattern: str, path: str) -> str: ...
    async def read(self, path: str) -> str: ...
    async def write(self, path: str, content: str) -> None: ...
    async def replace(self, path: str, old: str, new: str) -> None: ...
    async def run_code(self, code: str) -> ExecutionArtifact: ...
    async def close(self) -> None: ...


class E2BAnalysisWorkspace:
    """A lazily created E2B sandbox with read-only source conventions."""

    def __init__(self, *, timeout_seconds: int = 900) -> None:
        self._timeout_seconds = timeout_seconds
        self._sandbox: Sandbox | None = None

    async def _get_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = await asyncio.to_thread(
                Sandbox.create,
                timeout=self._timeout_seconds,
                allow_internet_access=False,
                metadata={"application": "generic-finance-agent"},
            )
            await asyncio.to_thread(
                self._sandbox.commands.run,
                f"mkdir -p {SOURCE_ROOT} {SCRATCH_ROOT}",
            )
        return self._sandbox

    @staticmethod
    def _checked_read_path(path: str) -> PurePosixPath:
        candidate = PurePosixPath(path)
        if not candidate.is_absolute():
            candidate = SOURCE_ROOT / candidate
        candidate = PurePosixPath(posixpath.normpath(str(candidate)))
        if not (
            candidate.is_relative_to(SOURCE_ROOT)
            or candidate.is_relative_to(SCRATCH_ROOT)
        ):
            raise ValueError("reads are limited to approved sources and scratch files")
        return candidate

    @staticmethod
    def _checked_scratch_path(path: str) -> PurePosixPath:
        candidate = PurePosixPath(path)
        if not candidate.is_absolute():
            candidate = SCRATCH_ROOT / candidate
        candidate = PurePosixPath(posixpath.normpath(str(candidate)))
        if not candidate.is_relative_to(SCRATCH_ROOT):
            raise ValueError("writes are limited to the scratch directory")
        return candidate

    async def seed_sources(self, sources: list[ApprovedSource]) -> None:
        sandbox = await self._get_sandbox()
        for source in sources:
            data = await asyncio.to_thread(Path(source.local_path).read_bytes)
            remote_path = SOURCE_ROOT / source.id / Path(source.name).name
            await asyncio.to_thread(
                sandbox.commands.run,
                f"mkdir -p {shlex.quote(str(remote_path.parent))}",
            )
            await asyncio.to_thread(sandbox.files.write, str(remote_path), data)

    async def glob(self, pattern: str) -> list[str]:
        pattern_path = PurePosixPath(pattern)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise ValueError("glob patterns must stay within approved sources")
        sandbox = await self._get_sandbox()
        command = (
            "python -c "
            + shlex.quote(
                "import glob,json; "
                f"print(json.dumps(glob.glob({str(SOURCE_ROOT / pattern)!r}, recursive=True)))"
            )
        )
        result = await asyncio.to_thread(sandbox.commands.run, command, timeout=20)
        return list(json.loads(result.stdout))

    async def grep(self, pattern: str, path: str = str(SOURCE_ROOT)) -> str:
        sandbox = await self._get_sandbox()
        checked_path = self._checked_read_path(path)
        command = (
            f"grep -R -n -E -- {shlex.quote(pattern)} {shlex.quote(str(checked_path))}"
        )
        result = await asyncio.to_thread(sandbox.commands.run, command, timeout=20)
        return result.stdout

    async def read(self, path: str) -> str:
        sandbox = await self._get_sandbox()
        checked_path = self._checked_read_path(path)
        return await asyncio.to_thread(sandbox.files.read, str(checked_path))

    async def write(self, path: str, content: str) -> None:
        sandbox = await self._get_sandbox()
        checked_path = self._checked_scratch_path(path)
        await asyncio.to_thread(sandbox.files.write, str(checked_path), content)

    async def replace(self, path: str, old: str, new: str) -> None:
        if not old:
            raise ValueError("old text must not be empty")
        checked_path = self._checked_scratch_path(path)
        content = await self.read(str(checked_path))
        occurrences = content.count(old)
        if occurrences != 1:
            raise ValueError(f"expected exactly one match, found {occurrences}")
        await self.write(str(checked_path), content.replace(old, new, 1))

    async def run_code(self, code: str) -> ExecutionArtifact:
        sandbox = await self._get_sandbox()
        artifact_id = f"artifact-{uuid4().hex}"
        script_path = SCRATCH_ROOT / f"{artifact_id}.py"
        await asyncio.to_thread(sandbox.files.write, str(script_path), code)
        result = await asyncio.to_thread(
            sandbox.commands.run,
            f"python {shlex.quote(str(script_path))}",
            cwd=str(SCRATCH_ROOT),
            timeout=60,
        )

        outputs: dict[str, object] = {}
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if lines:
            try:
                parsed = json.loads(lines[-1])
                if isinstance(parsed, dict):
                    outputs = parsed
            except json.JSONDecodeError:
                pass

        return ExecutionArtifact(
            id=artifact_id,
            code=code,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            outputs=outputs,
        )

    async def close(self) -> None:
        if self._sandbox is not None:
            sandbox = self._sandbox
            self._sandbox = None
            await asyncio.to_thread(sandbox.kill)

