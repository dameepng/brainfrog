"""Unified Multi-Model Catalog and OpenCode-Style Interactive Selector for BrainFrog.

Integrates Sumopod live model discovery with BrainFrog's curated provider catalog,
providing fuzzy search, direct provider/model selection, and interactive terminal navigation.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class ModelCatalogEntry:
    """Represents a selectable System 2 model in BrainFrog."""
    provider: str
    model_id: str
    display_name: str
    description: str
    context_limit: int
    is_live: bool = False
    owned_by: str = ""
    is_priority: bool = False
    is_verified: bool = True
    status: str = "available"  # "available", "unverified", "unavailable"

    @property
    def full_id(self) -> str:
        return f"{self.provider}/{self.model_id}"


SUMOPOD_PRIORITY_MODELS: List[str] = [
    "deepseek-v4-pro",
    "deepseek-v4.1-flash:netra",
    "glm-5.3-flash",
    "kimi-k3",
]

PRIORITY_MODEL_METADATA: Dict[str, Dict[str, Any]] = {
    "deepseek-v4-pro": {
        "display_name": "DeepSeek V4 Pro",
        "description": "DeepSeek flagship V4 pro reasoning",
        "context_limit": 64_000,
    },
    "deepseek-v4.1-flash:netra": {
        "display_name": "DeepSeek V4.1 Flash (Netra)",
        "description": "DeepSeek V4.1 Flash Netra variant",
        "context_limit": 64_000,
    },
    "glm-5.3-flash": {
        "display_name": "GLM-5.3 Flash",
        "description": "Zhipu GLM 5.3 ultra-fast low-latency",
        "context_limit": 128_000,
    },
    "kimi-k3": {
        "display_name": "Kimi K3",
        "description": "Moonshot Kimi K3 long-context",
        "context_limit": 128_000,
    },
}


def get_curated_provider_models() -> Dict[str, List[Tuple[str, str, str]]]:
    """Return the static curated provider models catalog."""
    from cli import PROVIDER_MODELS
    return PROVIDER_MODELS


def get_unified_model_catalog(
    active_provider: Optional[str] = None,
    include_live: bool = True,
) -> List[ModelCatalogEntry]:
    """Build a unified model catalog across all supported providers.

    Prioritizes key Sumopod models (deepseek-v4-pro, deepseek-v4.1-flash:netra,
    glm-5.3-flash, kimi-k3) at the top of the Sumopod section when confirmed
    available by the live catalog. If a priority model is absent from the live
    catalog, it is labeled unverified rather than claiming it is available.
    """
    from system2.claude_client import get_model_context_limit

    provider_models = get_curated_provider_models()
    providers_to_scan = (
        [active_provider] if active_provider and active_provider in provider_models
        else ["sumopod", "claude", "antigravity", "openai", "openrouter", "openai-compatible"]
    )

    catalog: List[ModelCatalogEntry] = []

    for prov in providers_to_scan:
        if prov == "sumopod":
            raw_models = None
            if include_live:
                try:
                    from system2 import System2Client
                    from system2.openai_client import OpenAISystem2Client

                    client = System2Client(provider="sumopod")
                    if isinstance(client, OpenAISystem2Client):
                        raw_models = client.list_models()
                except Exception:
                    raw_models = None

            if raw_models:
                live_map: Dict[str, Any] = {}
                for m in raw_models:
                    mid_val = m.get("id") if isinstance(m, dict) else m
                    if mid_val:
                        mid = str(mid_val)
                        live_map[mid] = m

                # 1. Confirmed Priority Models at top of Sumopod section
                for pid in SUMOPOD_PRIORITY_MODELS:
                    if pid in live_map:
                        m_obj = live_map[pid]
                        owner = m_obj.get("owned_by", "sumopod") if isinstance(m_obj, dict) else "sumopod"
                        catalog.append(
                            ModelCatalogEntry(
                                provider="sumopod",
                                model_id=pid,
                                display_name=pid,
                                description="Priority live model (Verified live)",
                                context_limit=get_model_context_limit(pid),
                                is_live=True,
                                owned_by=str(owner),
                                is_priority=True,
                                is_verified=True,
                                status="available",
                            )
                        )

                # 2. Other Live Models (Keep other live Sumopod models discoverable)
                for mid, m_obj in live_map.items():
                    if mid not in SUMOPOD_PRIORITY_MODELS:
                        owner = m_obj.get("owned_by", "sumopod") if isinstance(m_obj, dict) else "sumopod"
                        catalog.append(
                            ModelCatalogEntry(
                                provider="sumopod",
                                model_id=mid,
                                display_name=mid,
                                description="Live API model",
                                context_limit=get_model_context_limit(mid),
                                is_live=True,
                                owned_by=str(owner),
                                is_priority=False,
                                is_verified=True,
                                status="available",
                            )
                        )

                # 3. Priority Models absent from live catalog: labeled unverified
                for pid in SUMOPOD_PRIORITY_MODELS:
                    if pid not in live_map:
                        meta = PRIORITY_MODEL_METADATA.get(pid, {})
                        catalog.append(
                            ModelCatalogEntry(
                                provider="sumopod",
                                model_id=pid,
                                display_name=meta.get("display_name", pid),
                                description="Unverified (absent from live catalog)",
                                context_limit=get_model_context_limit(pid),
                                is_live=False,
                                owned_by="sumopod",
                                is_priority=True,
                                is_verified=False,
                                status="unverified",
                            )
                        )
                continue
            else:
                # Live fetch failed or credentials absent: Curated offline catalog fallback
                for mid, mname, mdesc in provider_models.get("sumopod", []):
                    limit = get_model_context_limit(mid)
                    catalog.append(
                        ModelCatalogEntry(
                            provider="sumopod",
                            model_id=mid,
                            display_name=mname,
                            description=mdesc,
                            context_limit=limit,
                            is_live=False,
                            owned_by="sumopod",
                            is_priority=False,
                            is_verified=True,
                            status="available",
                        )
                    )
                # For priority models, because live catalog is absent/unreachable, label unverified
                for pid in SUMOPOD_PRIORITY_MODELS:
                    meta = PRIORITY_MODEL_METADATA.get(pid, {})
                    catalog.append(
                        ModelCatalogEntry(
                            provider="sumopod",
                            model_id=pid,
                            display_name=meta.get("display_name", pid),
                            description="Unverified (offline / absent from live catalog)",
                            context_limit=get_model_context_limit(pid),
                            is_live=False,
                            owned_by="sumopod",
                            is_priority=True,
                            is_verified=False,
                            status="unverified",
                        )
                    )
                continue

        live_fetched = False

        if include_live and prov in ("openai", "openrouter", "openai-compatible"):
            try:
                from system2 import System2Client
                from system2.openai_client import OpenAISystem2Client

                client = System2Client(provider=prov)
                if isinstance(client, OpenAISystem2Client):
                    raw_models = client.list_models()
                    if raw_models:
                        for m in raw_models:
                            mid_val = m.get("id") if isinstance(m, dict) else m
                            if not mid_val:
                                continue
                            mid = str(mid_val)
                            owner = m.get("owned_by", prov) if isinstance(m, dict) else prov
                            limit = get_model_context_limit(mid)
                            catalog.append(
                                ModelCatalogEntry(
                                    provider=prov,
                                    model_id=mid,
                                    display_name=mid,
                                    description="Live API model",
                                    context_limit=limit,
                                    is_live=True,
                                    owned_by=str(owner),
                                    is_priority=False,
                                    is_verified=True,
                                    status="available",
                                )
                            )
                        live_fetched = True
            except Exception:
                live_fetched = False

        if not live_fetched:
            for mid, mname, mdesc in provider_models.get(prov, []):
                limit = get_model_context_limit(mid)
                catalog.append(
                    ModelCatalogEntry(
                        provider=prov,
                        model_id=mid,
                        display_name=mname,
                        description=mdesc,
                        context_limit=limit,
                        is_live=False,
                        owned_by=prov,
                        is_priority=False,
                        is_verified=True,
                        status="available",
                    )
                )

    return catalog


def search_model_catalog(
    catalog: List[ModelCatalogEntry],
    query: str,
) -> List[ModelCatalogEntry]:
    """Filter model catalog by query across model ID, provider, name, and description."""
    q = query.strip().lower()
    if not q:
        return list(catalog)

    matches: List[Tuple[int, int, ModelCatalogEntry]] = []
    for entry in catalog:
        fid = entry.full_id.lower()
        mid = entry.model_id.lower()
        prov = entry.provider.lower()
        name = entry.display_name.lower()
        desc = entry.description.lower()

        # Ranking score: lower is higher priority
        if fid == q or mid == q:
            score = 0
        elif fid.startswith(q) or mid.startswith(q):
            score = 1
        elif q in mid or q in fid:
            score = 2
        elif prov == q or prov.startswith(q):
            score = 3
        elif q in name or q in desc:
            score = 4
        else:
            continue

        priority_boost = 0 if getattr(entry, "is_priority", False) and entry.is_live else 1
        matches.append((score, priority_boost, entry))

    matches.sort(key=lambda x: (x[0], x[1]))
    return [entry for _, _, entry in matches]


def resolve_model_selection(
    selection: str,
    catalog: List[ModelCatalogEntry],
    current_provider: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve user model selection string into (provider, model_id, error_message).

    Supports:
    - '<provider>/<model_id>' (e.g. 'sumopod/deepseek-chat')
    - '<model_id>' (e.g. 'deepseek-chat' or 'claude-sonnet-5')
    - 1-based integer index corresponding to current catalog position
    """
    raw = selection.strip()
    if not raw:
        return None, None, "Pilihan model tidak boleh kosong."

    valid_providers = {"sumopod", "claude", "antigravity", "openai", "openrouter", "openai-compatible"}

    # 1. 1-based numeric selection
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(catalog):
            chosen = catalog[idx - 1]
            return chosen.provider, chosen.model_id, None
        return None, None, f"Nomor model '{raw}' tidak valid. Pilihan: 1–{len(catalog)}."

    # 2. Direct provider/model syntax: <provider>/<model_id>
    if "/" in raw:
        parts = raw.split("/", 1)
        prov = parts[0].strip().lower()
        mid = parts[1].strip()

        if prov not in valid_providers:
            return None, None, f"Provider '{prov}' tidak valid. Pilihan: {', '.join(sorted(valid_providers))}."

        if not mid:
            return None, None, f"Format tidak lengkap: '{raw}'. Masukkan: <provider>/<model-id>."

        # Verify model exists for that provider in catalog
        matching = [e for e in catalog if e.provider == prov and e.model_id.lower() == mid.lower()]
        if matching:
            return prov, matching[0].model_id, None

        # For providers that allow custom / dynamic model strings, accept if syntax is non-empty
        if prov in ("sumopod", "openai", "openrouter", "openai-compatible"):
            return prov, mid, None

        return None, None, f"Model '{mid}' tidak ditemukan untuk provider '{prov}'."

    # 3. Bare model_id syntax: check current_provider first
    low_raw = raw.lower()
    in_current = [e for e in catalog if e.provider == current_provider and e.model_id.lower() == low_raw]
    if in_current:
        return current_provider, in_current[0].model_id, None

    # Search all providers for a match
    all_matches = [e for e in catalog if e.model_id.lower() == low_raw]
    if len(all_matches) == 1:
        return all_matches[0].provider, all_matches[0].model_id, None
    elif len(all_matches) > 1:
        provs = [e.provider for e in all_matches]
        return (
            None,
            None,
            f"Model '{raw}' ambigu karena tersedia di beberapa provider ({', '.join(provs)}). "
            f"Gunakan format spesifik: /model {provs[0]}/{raw}",
        )

    return (
        None,
        None,
        f"Model '{raw}' tidak ditemukan. Gunakan `/model search <query>` atau `/model` untuk melihat daftar model.",
    )


def _read_terminal_key() -> Tuple[str, Optional[str]]:
    """Read a single keypress from terminal.

    Returns:
        (key_type, char_value)
        key_type can be: "UP", "DOWN", "ENTER", "ESCAPE", "BACKSPACE", "CHAR", "EOF", "OTHER"
    """
    if not sys.stdin.isatty():
        return "EOF", None

    if sys.platform == "win32":
        try:
            import msvcrt
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch2 = msvcrt.getwch()
                if ch2 == "H":
                    return "UP", None
                elif ch2 == "P":
                    return "DOWN", None
                elif ch2 == "K":
                    return "LEFT", None
                elif ch2 == "M":
                    return "RIGHT", None
                return "OTHER", ch2
            elif ch in ("\r", "\n"):
                return "ENTER", None
            elif ch == "\x1b":  # ESC
                return "ESCAPE", None
            elif ch in ("\x08", "\x7f"):  # Backspace
                return "BACKSPACE", None
            elif ch == "\x03":  # Ctrl+C
                return "ESCAPE", None
            elif ch.isprintable():
                return "CHAR", ch
            return "OTHER", ch
        except Exception:
            return "EOF", None
    else:
        try:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    # Escape sequence
                    seq = sys.stdin.read(2)
                    if seq == "[A":
                        return "UP", None
                    elif seq == "[B":
                        return "DOWN", None
                    return "ESCAPE", None
                elif ch in ("\r", "\n"):
                    return "ENTER", None
                elif ch in ("\x7f", "\x08"):
                    return "BACKSPACE", None
                elif ch == "\x03":
                    return "ESCAPE", None
                elif ch.isprintable():
                    return "CHAR", ch
                return "OTHER", ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception:
            return "EOF", None


def select_model_opencode(
    current_provider: str,
    current_model: str,
    initial_query: str = "",
    catalog: Optional[List[ModelCatalogEntry]] = None,
    key_input_fn: Optional[Callable[[], Tuple[str, Optional[str]]]] = None,
    max_visible: int = 8,
) -> Optional[Tuple[str, str]]:
    """OpenCode-inspired interactive terminal model selector.

    Displays current provider and model, real-time search filtering, Up/Down arrow navigation,
    Enter to select, and Esc to cancel. Erases the selector frame on exit so terminal history
    remains completely clean.
    """
    from rich.align import Align
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from cli import get_layout_dims, _erase_lines, COLOR_ACCENT, COLOR_FG_PRIMARY, COLOR_FG_SECONDARY, COLOR_FG_MUTED, COLOR_INFO, COLOR_BG_SURFACE

    console = Console()
    full_catalog = catalog if catalog is not None else get_unified_model_catalog(include_live=True)

    if not full_catalog:
        console.print("[yellow]Tidak ada model yang ditemukan dalam katalog.[/yellow]")
        return None

    # Handle non-interactive or piped terminal environments safely
    is_interactive = (sys.stdin.isatty() if key_input_fn is None else True)
    if not is_interactive:
        cols, rows, box_w, margin, pad = get_layout_dims()
        table = Table(
            title=f" 🤖 BrainFrog Models · Active: {current_provider}/{current_model} ",
            box=box.ROUNDED,
            border_style=COLOR_FG_MUTED,
            header_style=f"bold {COLOR_ACCENT}",
            width=box_w,
        )
        table.add_column("Provider / Model ID", style=f"bold {COLOR_FG_PRIMARY}", width=38)
        table.add_column("Source", style=COLOR_INFO, width=14)
        table.add_column("Context Limit", style=COLOR_FG_SECONDARY, width=18)
        table.add_column("Description", style=COLOR_FG_MUTED)

        for entry in full_catalog[:20]:
            if entry.status in ("unverified", "unavailable") or not entry.is_verified:
                src_tag = f"[bold yellow]{entry.status.capitalize()}[/bold yellow]"
            elif entry.is_live:
                src_tag = "[bold green]Live API ★[/bold green]" if getattr(entry, "is_priority", False) else "[bold cyan]Live API[/bold cyan]"
            else:
                src_tag = "[dim]Catalog[/dim]"
            table.add_row(entry.full_id, src_tag, f"{entry.context_limit:,} tokens", entry.description)

        table.caption = f"[{COLOR_FG_MUTED}]Terminal non-interaktif terdeteksi. Gunakan: /model <provider>/<model-id>[/{COLOR_FG_MUTED}]"
        console.print()
        console.print(Align.center(table) if cols > 100 else table)
        console.print()
        return None

    query = initial_query
    selected_idx = 0
    scroll_offset = 0

    # Locate current model index if no initial query
    if not initial_query:
        for idx, e in enumerate(full_catalog):
            if e.provider == current_provider and e.model_id == current_model:
                selected_idx = idx
                break

    lines_rendered = 0
    read_key = key_input_fn or _read_terminal_key

    while True:
        filtered = search_model_catalog(full_catalog, query)
        total_filtered = len(filtered)

        if total_filtered == 0:
            selected_idx = 0
            scroll_offset = 0
        else:
            selected_idx = max(0, min(total_filtered - 1, selected_idx))
            if selected_idx < scroll_offset:
                scroll_offset = selected_idx
            elif selected_idx >= scroll_offset + max_visible:
                scroll_offset = selected_idx - max_visible + 1

        cols, rows, box_w, margin, pad = get_layout_dims()

        table = Table(
            title=f" 🤖 BrainFrog Model Switcher · Current: [bold {COLOR_ACCENT}]{current_provider}/{current_model}[/bold {COLOR_ACCENT}] ",
            box=box.ROUNDED,
            border_style=COLOR_FG_MUTED,
            header_style=f"bold {COLOR_ACCENT}",
            show_header=True,
            padding=(0, 1),
            width=box_w,
        )
        table.add_column("", width=2, justify="center")
        table.add_column("Provider / Model ID", style=f"bold {COLOR_FG_PRIMARY}", width=38)
        table.add_column("Source", width=13)
        table.add_column("Context", style=COLOR_FG_SECONDARY, width=15)
        table.add_column("Description", style=COLOR_FG_MUTED)

        visible_slice = filtered[scroll_offset : scroll_offset + max_visible]
        for offset_i, entry in enumerate(visible_slice):
            abs_idx = scroll_offset + offset_i
            is_cursor = (abs_idx == selected_idx)
            is_current = (entry.provider == current_provider and entry.model_id == current_model)

            pointer = f"[bold {COLOR_ACCENT}]▸[/bold {COLOR_ACCENT}]" if is_cursor else " "
            full_label = f"[{COLOR_INFO}]{entry.provider}[/{COLOR_INFO}]/[bold {COLOR_FG_PRIMARY}]{entry.model_id}[/bold {COLOR_FG_PRIMARY}]"

            if entry.status in ("unverified", "unavailable") or not entry.is_verified:
                src_tag = f"[bold yellow]{entry.status.capitalize()}[/bold yellow]"
            elif entry.is_live:
                if getattr(entry, "is_priority", False):
                    src_tag = f"[bold green]Live ★[/bold green]"
                else:
                    src_tag = f"[bold {COLOR_INFO}]Live[/bold {COLOR_INFO}]"
            else:
                src_tag = f"[{COLOR_FG_MUTED}]Catalog[/{COLOR_FG_MUTED}]"

            ctx_tag = f"{entry.context_limit:,} tok"
            active_badge = f" [bold {COLOR_ACCENT}]● active[/bold {COLOR_ACCENT}]" if is_current else ""
            desc = f"{'★ ' if getattr(entry, 'is_priority', False) and entry.is_live else ''}{entry.description}{active_badge}"

            row_style = f"on {COLOR_BG_SURFACE}" if is_cursor else None
            table.add_row(pointer, full_label, src_tag, ctx_tag, desc, style=row_style)

        if total_filtered == 0:
            table.add_row("", f"[yellow]Tidak ada model cocok dengan '{query}'[/yellow]", "", "", "")

        filter_prompt = f"Search: [bold {COLOR_INFO}]{query}[/bold {COLOR_INFO}]█  [dim](↑/↓ pilih · Enter konfirmasi · Esc batal · ketik untuk filter)[/dim]"
        footer_info = f"[dim]Menampilkan {len(visible_slice)} dari {total_filtered} model[/dim]"
        table.caption = f"{filter_prompt}\n{footer_info}"

        if lines_rendered > 0:
            _erase_lines(lines_rendered)

        console.print()
        console.print(Align.center(table) if cols > 100 else table)
        console.print()

        # Count total rendered lines: table height + blank lines + caption
        # Table rows + 4 (borders/header) + 4 (title/padding/caption) + 2 (blank lines)
        rendered_line_count = len(visible_slice) + (1 if total_filtered == 0 else 0) + 10
        lines_rendered = rendered_line_count

        ktype, kval = read_key()

        if ktype in ("ESCAPE", "EOF"):
            _erase_lines(lines_rendered)
            return None
        elif ktype == "ENTER":
            _erase_lines(lines_rendered)
            if filtered:
                chosen = filtered[selected_idx]
                return chosen.provider, chosen.model_id
            return None
        elif ktype == "UP":
            if total_filtered > 0:
                selected_idx = (selected_idx - 1) % total_filtered
        elif ktype == "DOWN":
            if total_filtered > 0:
                selected_idx = (selected_idx + 1) % total_filtered
        elif ktype == "BACKSPACE":
            if query:
                query = query[:-1]
                selected_idx = 0
                scroll_offset = 0
        elif ktype == "CHAR" and kval:
            query += kval
            selected_idx = 0
            scroll_offset = 0
