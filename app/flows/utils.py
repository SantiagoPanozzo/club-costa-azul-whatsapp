from ..state import Session
from ..storing_client import storing_client as whatsapp_client

WHATSAPP_LIST_PAGE_SIZE = 10


async def send_list_pages(
    phone: str,
    *,
    body: str,
    button_text: str,
    rows: list[dict],
    section_title: str,
    session: Session,
) -> None:
    """Send every row in WhatsApp-sized pages without silently hiding results."""
    pages = [rows[index : index + WHATSAPP_LIST_PAGE_SIZE] for index in range(0, len(rows), WHATSAPP_LIST_PAGE_SIZE)]
    for index, page in enumerate(pages, start=1):
        suffix = f" ({index}/{len(pages)})" if len(pages) > 1 else ""
        await whatsapp_client.send_list(
            to=phone,
            body=f"{body}{suffix}",
            button_text=button_text,
            rows=page,
            section_title=section_title,
            session=session,
        )
