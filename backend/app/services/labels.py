"""Label service: Gmail color palette + resolving a Label to a live Gmail
label id (creating/coloring it lazily).

Gmail's labels.create/patch only accept colors from a fixed allowed palette;
offering anything else returns HTTP 400. We expose a curated subset of that
palette and degrade gracefully (create the label uncolored) if Gmail rejects
a color, so a bad palette entry can never block labeling.
"""

from sqlalchemy.orm import Session

from app.logging_setup import get_logger
from app.models import Label
from app.services.gmail import GmailClient, GmailError

log = get_logger(__name__)

# Curated subset of Gmail's allowed label colors as {background, text} hex pairs.
GMAIL_PALETTE: list[dict[str, str]] = [
    {"background": "#ffffff", "text": "#000000"},
    {"background": "#cccccc", "text": "#000000"},
    {"background": "#999999", "text": "#ffffff"},
    {"background": "#666666", "text": "#ffffff"},
    {"background": "#434343", "text": "#ffffff"},
    {"background": "#000000", "text": "#ffffff"},
    {"background": "#fb4c2f", "text": "#ffffff"},  # red
    {"background": "#ffad47", "text": "#000000"},  # orange
    {"background": "#fad165", "text": "#000000"},  # yellow
    {"background": "#16a766", "text": "#ffffff"},  # green
    {"background": "#43d692", "text": "#000000"},  # mint
    {"background": "#4a86e8", "text": "#ffffff"},  # blue
    {"background": "#a479e2", "text": "#ffffff"},  # purple
    {"background": "#f691b3", "text": "#000000"},  # pink
    {"background": "#b99aff", "text": "#000000"},  # lavender
    {"background": "#7a4706", "text": "#ffffff"},  # brown
    {"background": "#fbe983", "text": "#000000"},  # light yellow
    {"background": "#b3efd3", "text": "#000000"},  # light green
    {"background": "#a4c2f4", "text": "#000000"},  # light blue
    {"background": "#fbc8d9", "text": "#000000"},  # light pink
    {"background": "#e7e7e7", "text": "#000000"},  # light gray
    {"background": "#285bac", "text": "#ffffff"},  # dark blue
    {"background": "#653e9b", "text": "#ffffff"},  # dark purple
    {"background": "#822111", "text": "#ffffff"},  # dark red
]


def is_allowed_color(text_color: str | None, background_color: str | None) -> bool:
    if text_color is None and background_color is None:
        return True
    return any(p["text"] == text_color and p["background"] == background_color
               for p in GMAIL_PALETTE)


def color_dict(label: Label) -> dict | None:
    if label.text_color and label.background_color:
        return {"textColor": label.text_color,
                "backgroundColor": label.background_color}
    return None


async def ensure_gmail_label(client: GmailClient, cache: dict, label: Label) -> str:
    """Resolve a Label to a live Gmail label id, creating it (with color) on
    first use. Sets label.gmail_label_id in memory (caller persists)."""
    if label.gmail_label_id:
        return label.gmail_label_id
    if "names" not in cache:
        cache["names"] = {lb["name"]: lb["id"] for lb in await client.list_labels()}
    names: dict[str, str] = cache["names"]
    if label.name in names:
        label.gmail_label_id = names[label.name]
        return label.gmail_label_id

    color = color_dict(label)
    try:
        created = await client.create_label(label.name, color)
    except GmailError:
        if not color:
            raise
        log.warning("label_color_rejected_retrying_plain", label=label.name)
        created = await client.create_label(label.name, None)
    names[label.name] = created["id"]
    label.gmail_label_id = created["id"]
    return label.gmail_label_id


async def sync_label_to_gmail(session: Session, client: GmailClient,
                              label: Label) -> None:
    """Create-or-update the Gmail label to match name+color (best effort)."""
    cache: dict = {}
    if label.gmail_label_id:
        try:
            await client.patch_label(label.gmail_label_id, name=label.name,
                                     color=color_dict(label))
        except GmailError as e:
            log.warning("label_patch_failed", label=label.name, error=str(e))
    else:
        await ensure_gmail_label(client, cache, label)
