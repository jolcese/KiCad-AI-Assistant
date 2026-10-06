"""Electrical Rules Check (ERC) tools for the KiCad MCP server.

Runs KiCad's schematic ERC headlessly via ``kicad-cli sch erc`` and returns
the violations as structured data the assistant can read and act on (unlike
the PCB DRC tool, which only opens a dialog over the IPC API). Requires
``kicad-cli`` on PATH or ``KICAD_CLI_PATH`` set; it does not use the KiCad
IPC socket.
"""

import json
import logging
import os
from typing import Any

from fastmcp import Context, FastMCP

from kcaa.utils.kicad_cli import KiCadCLIError
from kcaa.utils.secure_subprocess import create_temp_file, run_kicad_command_async

log = logging.getLogger(__name__)

# severity keyword -> kicad-cli flags. "default" uses KiCad's own default
# (errors + warnings); "all" additionally includes excluded violations.
_SEVERITY_FLAGS: dict[str, list[str]] = {
    "default": ["--severity-error", "--severity-warning"],
    "all": ["--severity-all"],
    "error": ["--severity-error"],
    "warning": ["--severity-warning"],
}


def _flatten_violations(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the per-sheet violations of a kicad-cli ERC JSON report."""
    violations: list[dict[str, Any]] = []
    for sheet in report.get("sheets", []) or []:
        sheet_path = sheet.get("path", "/")
        for v in sheet.get("violations", []) or []:
            items = []
            for it in v.get("items", []) or []:
                pos = it.get("pos") or {}
                items.append(
                    {
                        "description": it.get("description", ""),
                        "x": pos.get("x"),
                        "y": pos.get("y"),
                        "uuid": it.get("uuid", ""),
                    }
                )
            violations.append(
                {
                    "type": v.get("type", ""),
                    "severity": v.get("severity", ""),
                    "description": v.get("description", ""),
                    "sheet": sheet_path,
                    "items": items,
                }
            )
    return violations


def register_erc_tools(mcp: FastMCP) -> None:
    """Register ERC tools against *mcp*."""

    @mcp.tool()
    async def run_erc(
        schematic_path: str,
        severity: str = "default",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run KiCad's Electrical Rules Check (ERC) on a schematic.

        Runs ``kicad-cli sch erc`` headlessly (no GUI, no IPC socket needed)
        and returns the violations as structured data. The check follows the
        full sheet hierarchy from *schematic_path*.

        Args:
            schematic_path: Absolute path to the root .kicad_sch file.
            severity: Which violations to report:
                "default" (errors + warnings), "all" (also excluded),
                "error" (errors only), or "warning" (warnings only).

        Returns:
            dict with keys: success (bool), violation_count (int),
            error_count, warning_count, exclusion_count, violations (list of
            {type, severity, description, sheet, items:[{description, x, y,
            uuid}]}), kicad_version, source, coordinate_units. On failure:
            {success: False, error: str}.
        """
        if not schematic_path.endswith(".kicad_sch"):
            return {"success": False, "error": f"Not a .kicad_sch file: {schematic_path!r}"}
        if not os.path.isfile(schematic_path):
            return {"success": False, "error": f"Schematic file not found: {schematic_path!r}"}
        flags = _SEVERITY_FLAGS.get(severity)
        if flags is None:
            return {
                "success": False,
                "error": f"severity must be one of {sorted(_SEVERITY_FLAGS)} (got {severity!r})",
            }

        out_path = create_temp_file(suffix=".json", prefix="kcaa_erc_")
        # Deliberately omit --exit-code-violations so a schematic with
        # violations still exits 0; a nonzero code then signals a real failure.
        command = [
            "sch",
            "erc",
            schematic_path,
            "--format",
            "json",
            "--output",
            out_path,
            *flags,
        ]

        try:
            try:
                result = await run_kicad_command_async(
                    command,
                    input_files=[schematic_path],
                    output_files=[out_path],
                    timeout=120.0,
                )
            except KiCadCLIError as exc:
                return {"success": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 — surface CLI/subprocess failures
                return {"success": False, "error": f"Failed to run ERC: {exc}"}

            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                return {
                    "success": False,
                    "error": f"kicad-cli ERC failed (exit {result.returncode}): {stderr[:300]}",
                }

            try:
                with open(out_path, encoding="utf-8") as f:
                    report = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                return {"success": False, "error": f"Could not read ERC report: {exc}"}
        finally:
            # Always clean up the temp report, including on the error returns above.
            try:
                os.unlink(out_path)
            except OSError:
                pass

        violations = _flatten_violations(report)
        by_severity: dict[str, int] = {}
        for v in violations:
            by_severity[v["severity"]] = by_severity.get(v["severity"], 0) + 1

        return {
            "success": True,
            "violation_count": len(violations),
            "error_count": by_severity.get("error", 0),
            "warning_count": by_severity.get("warning", 0),
            "exclusion_count": by_severity.get("exclusion", 0),
            "violations": violations,
            "kicad_version": report.get("kicad_version"),
            "source": report.get("source"),
            "coordinate_units": report.get("coordinate_units"),
        }
