import streamlit as st
from pathlib import Path

from src.connector_intelligence.config import Settings
from src.connector_intelligence.db import connect, count_source_records, read_source_records
from src.connector_intelligence.query import answer
from src.connector_intelligence.citation_display import (
    format_source_location,
    prepare_citation_display,
    prepare_facet_display,
)

st.set_page_config(page_title="E9Rental Connector Intelligence", layout="wide")
st.markdown("""
<style>
    :root { color-scheme: light; }
    [data-testid="stAppViewContainer"] { background: #f7f9fc; color: #172b45; }
    [data-testid="stHeader"] { background: rgba(247, 249, 252, 0.94); }
    .block-container { max-width: 1440px; padding-top: 1.15rem; padding-bottom: 2.2rem; }
    [data-testid="stSidebar"] { background: #f0f4f9; border-right: 1px solid #dce4ef; }
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: 0.65rem; }
    h1, h2, h3, h4,
    [data-testid="stHeading"] h1, [data-testid="stHeading"] h2,
    [data-testid="stHeading"] h3, [data-testid="stHeading"] h4,
    [data-testid="stMarkdownContainer"] h1, [data-testid="stMarkdownContainer"] h2,
    [data-testid="stMarkdownContainer"] h3, [data-testid="stMarkdownContainer"] h4 {
        color: #172b45 !important; letter-spacing: -0.02em;
    }
    p, li, label, [data-testid="stCaptionContainer"] { color: #5b6b80; }
    [data-testid="stMetric"] { background: #fff; border: 1px solid #dce4ef;
        border-radius: 12px; padding: 1rem 1rem; min-height: 6.1rem;
        box-shadow: 0 2px 8px rgba(20, 43, 73, 0.035); }
    [data-testid="stMetricValue"], [data-testid="stMetricValue"] div {
        color: #172b45; font-size: clamp(1.05rem, 1.4vw, 1.55rem); line-height: 1.35;
        white-space: normal !important; overflow: visible !important; text-overflow: clip !important;
        overflow-wrap: anywhere !important; word-break: normal; }
    [data-testid="stMetricLabel"] { color: #5b6b80; line-height: 1.35; }
    [data-testid="stVerticalBlockBorderWrapper"] { background: #fff; border-color: #dce4ef; border-radius: 14px; }
    [data-testid="stTextInput"] input { min-height: 3rem; border-radius: 10px; }
    .stButton > button, [data-testid="stFormSubmitButton"] button {
        border-radius: 9px; font-weight: 600; border: 1px solid #cbd7e5;
        background: #fff; color: #24415f; }
    .stButton > button:hover {
        background: #eaf2fb; color: #173d65; border-color: #a9c4e0; }
    [data-testid="stFormSubmitButton"] button[kind="primary"],
    [data-testid="stFormSubmitButton"] button[data-testid="stBaseButton-primary"] {
        background: #1769aa; color: #fff; border-color: #1769aa;
    }
    [data-testid="stFormSubmitButton"] button[kind="primary"]:hover,
    [data-testid="stFormSubmitButton"] button[data-testid="stBaseButton-primary"]:hover {
        background: #12598f; border-color: #12598f; color: #fff;
    }
    [data-testid="stDataFrame"] { border: 1px solid #dce4ef; border-radius: 10px; overflow: hidden; }
    [data-testid="stDataFrame"] [role="columnheader"] {
        background: #edf3f9; color: #172b45; font-weight: 700;
    }
    [data-testid="stDataFrame"] [role="gridcell"] { border-bottom: 1px solid #e8edf3; }
    [data-testid="stAlert"] { border-radius: 10px; }
    [data-testid="stAlert"] [data-testid="stMarkdownContainer"] p { color: #35465a; }
    [data-testid="stVerticalBlock"] { gap: 0.8rem; }
    .stTabs [data-baseweb="tab-list"] { gap: 0.35rem; border-bottom: 1px solid #dce4ef; }
    .stTabs [data-baseweb="tab"] { color: #5b6b80; padding: 0.65rem 0.9rem; }
    .stTabs [aria-selected="true"] { color: #1769aa; }
    .e9-eyebrow { color: #1769aa; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.12em; }
    .e9-subtitle { color: #5b6b80; margin-top: -0.45rem; font-size: 1.05rem; }
</style>
""", unsafe_allow_html=True)
st.markdown(
    '<div class="e9-eyebrow">RENTAL BUSINESS INTELLIGENCE</div>'
    '<h1>E9Rental Connector Intelligence</h1>'
    '<div class="e9-subtitle">Your business information, connected and searchable.</div>',
    unsafe_allow_html=True,
)
settings = Settings()
client_file_available = Path(settings.google_client_secrets_file).is_file()
gmail_account_configured = bool(settings.gmail_mailbox_owner.strip())
drive_account_configured = bool(settings.google_drive_account.strip())
with st.expander(
    "Set up the Gmail and Google Drive sandbox connections",
    expanded=not (client_file_available and gmail_account_configured and drive_account_configured),
):
    st.info(
        "Gmail and Google Drive are the ready-to-use connectors in this project. "
        "They use separate read-only OAuth grants and tokens. No account is contacted "
        "until you press a Sync button below."
    )
    status_columns = st.columns(3)
    status_columns[0].write("**OAuth desktop client**")
    status_columns[0].write("Available" if client_file_available else "Not found")
    status_columns[1].write("**Gmail sandbox address**")
    status_columns[1].write("Configured" if gmail_account_configured else "Not configured")
    status_columns[2].write("**Drive sandbox address**")
    status_columns[2].write("Configured" if drive_account_configured else "Not configured")
    if not client_file_available or not gmail_account_configured or not drive_account_configured:
        st.markdown(
            "1. Get the organizer-authorized Google sandbox account(s) and OAuth desktop client JSON.\n"
            "2. Save the client JSON at the path in `GOOGLE_CLIENT_SECRETS_FILE` (default `credentials.json`).\n"
            "3. Copy `.env.example` to `.env`; set `GMAIL_MAILBOX_OWNER` and `GOOGLE_DRIVE_ACCOUNT` to the exact authorized account addresses.\n"
            "4. Restart Streamlit, then use **Sync Gmail (read-only)** and **Sync Drive (read-only)** in their tabs."
        )
        st.caption(
            "The app checks the signed-in account against each configured address before fetching data. "
            "Gmail and Drive authorization are separate; only the connector you explicitly sync is accessed."
        )
        st.caption("Restart the app after changing `.env`; values are read when the app starts.")
with connect(settings.database_path) as conn:
    selected_whatsapp_groups = settings.selected_whatsapp_groups()
    stats = {s: count_source_records(conn, s, selected_whatsapp_groups)
             for s in ("whatsapp", "gmail", "drive")}
    with st.sidebar:
        st.markdown("## ◇ E9Rental")
        st.caption("Connector Intelligence")
        st.divider()
        st.markdown("#### Data sources")
        gmail_status = "Configured" if client_file_available and gmail_account_configured else "Setup needed"
        drive_status = "Configured" if client_file_available and drive_account_configured else "Setup needed"
        whatsapp_status = "Groups selected" if selected_whatsapp_groups else "No groups selected"
        for label, state, count in (
            ("Google Drive", drive_status, stats["drive"]),
            ("Gmail", gmail_status, stats["gmail"]),
            ("WhatsApp", whatsapp_status, stats["whatsapp"]),
        ):
            with st.container(border=True):
                st.markdown(f"**{label}**")
                st.caption(f"{state} · {count} stored records")
        st.divider()
        st.success("Database connected")
        st.caption("Local folder import is available in the Google Drive workspace tab.")
    tabgm, tabdr, tabwa, tabq = st.tabs(["Gmail", "Google Drive", "WhatsApp (optional)", "Ask your business"])
    for tab, source in ((tabgm, "gmail"), (tabdr, "drive"), (tabwa, "whatsapp")):
        with tab:
            if source == "whatsapp" and not selected_whatsapp_groups:
                st.info("No WhatsApp groups are currently selected, so no WhatsApp message records are displayed.")
            if source == "gmail" and st.button("Sync Gmail (read-only)", key="gmail_sync"):
                from src.connector_intelligence.connectors.gmail import (
                    GmailConnectorError,
                    get_gmail_service,
                    sync_gmail,
                )

                if not settings.gmail_mailbox_owner:
                    st.error("Set GMAIL_MAILBOX_OWNER to the authorized sandbox mailbox in your environment or .env file.")
                else:
                    try:
                        service = get_gmail_service(settings)
                        result = sync_gmail(
                            conn,
                            service,
                            mailbox_owner=settings.gmail_mailbox_owner,
                            reference_date=settings.reference_day(),
                            output_dir=settings.output_dir,
                        )
                        st.success(
                            f"Fetched {result['messages_fetched']} messages across {result['threads_fetched']} threads; "
                            f"{result['unanswered_inbound']} unanswered inbound and "
                            f"{result['important_attachments']} important attachments flagged."
                        )
                        st.rerun()
                    except GmailConnectorError as exc:
                        st.error(str(exc))
            if source == "drive" and st.button("Sync Drive (read-only)", key="drive_sync"):
                from src.connector_intelligence.connectors.drive import DriveConnectorError, get_drive_service
                from src.connector_intelligence.connectors.drive_store import sync_drive

                if not settings.google_drive_account:
                    st.error("Set GOOGLE_DRIVE_ACCOUNT to the authorized sandbox account in your environment or .env file.")
                else:
                    try:
                        service = get_drive_service(settings)
                        result = sync_drive(
                            conn,
                            service,
                            expected_account=settings.google_drive_account,
                            output_dir=settings.output_dir,
                        )
                        st.success(
                            f"Listed {result['files_listed']} files; "
                            f"{result.get('extracted_files', 0)} extracted, "
                            f"{result.get('unsupported_files', 0)} unsupported, "
                            f"{result.get('api_error_files', 0) + result.get('inaccessible_files', 0)} with access/API errors."
                        )
                        st.rerun()
                    except DriveConnectorError as exc:
                        st.error(str(exc))
            if source == "drive":
                st.subheader("Google Drive Local Import")
                st.caption(
                    "Import supported documents from a local folder into the existing Drive records. "
                    "Only extracted text and relative file paths are stored."
                )
                folder_path = st.text_input(
                    "Local folder path",
                    value="data/imports/drive_sample",
                    key="local_drive_folder_path",
                )
                root_label = st.text_input(
                    "Root label",
                    value="drive-sample",
                    key="local_drive_root_label",
                )
                st.session_state.setdefault("local_drive_import_report", None)
                st.session_state.setdefault("local_drive_import_error", None)
                if st.button("Import Documents", key="local_drive_import_button"):
                    st.session_state["local_drive_import_report"] = None
                    st.session_state["local_drive_import_error"] = None
                    candidate = Path(folder_path).expanduser() if folder_path.strip() else None
                    if candidate is None:
                        st.session_state["local_drive_import_error"] = "Enter a local folder path."
                    elif not root_label.strip():
                        st.session_state["local_drive_import_error"] = "Enter a root label."
                    else:
                        try:
                            if candidate.is_symlink():
                                st.session_state["local_drive_import_error"] = "The selected folder cannot be a symbolic link."
                            elif not candidate.exists():
                                st.session_state["local_drive_import_error"] = "The selected folder was not found or cannot be accessed."
                            elif not candidate.is_dir():
                                st.session_state["local_drive_import_error"] = "The selected path is not a folder."
                            else:
                                from src.connector_intelligence.connectors.local_drive import import_local_folder

                                with st.spinner("Scanning, extracting, classifying, saving, and exporting documents?"):
                                    report = import_local_folder(candidate, root_label, conn)
                                    from src.connector_intelligence.exports import export_source

                                    export_source(
                                        conn,
                                        "drive",
                                        Path(settings.output_dir) / "drive_output.json",
                                    )
                                st.session_state["local_drive_import_report"] = report
                                st.rerun()
                        except Exception:
                            st.session_state["local_drive_import_error"] = (
                                "The folder could not be imported. Check the folder permissions and try again."
                            )
                if st.session_state["local_drive_import_error"]:
                    st.error(st.session_state["local_drive_import_error"])
                report = st.session_state["local_drive_import_report"]
                if report:
                    counts = report["counts"]
                    metric_values = (
                        ("Total discovered", report["files_scanned"]),
                        ("Successfully imported", counts.get("imported", 0)),
                        ("Already imported", counts.get("already_imported", 0)),
                        ("Unsupported", counts.get("unsupported", 0)),
                        ("Empty / no text", counts.get("empty", 0) + counts.get("no_text", 0)),
                        ("Extraction errors", counts.get("extraction_error", 0)),
                        ("Classification errors", counts.get("classification_error", 0)),
                        ("Storage errors", counts.get("storage_error", 0)),
                    )
                    metric_columns = st.columns(4)
                    for index, (label, value) in enumerate(metric_values):
                        metric_columns[index % 4].metric(label, value)
                    st.dataframe(
                        [{
                            "Filename": item["filename"],
                            "Relative path": item["relative_path"],
                            "Category": item["category"] or "",
                            "Import status": item["status"],
                            "Error": item["error_message"] or "",
                        } for item in report["results"]],
                        use_container_width=True,
                        hide_index=True,
                    )
            rows = read_source_records(conn, source, selected_whatsapp_groups)
            st.dataframe([dict(r) for r in rows], use_container_width=True)
    with tabq:
        st.subheader("Ask your business anything")
        st.caption("Ask a question in everyday language. Answers are based on evidence in your connected records.")
        st.markdown("**Try an example**")
        examples = (
            "What is the rental duration?",
            "What is the quotation amount?",
            "Show the marketing campaign budget.",
            "Which campaigns exceeded their budget?",
            "What is the customer's verification status?",
            "Which documents are pending?",
        )

        def choose_example(value):
            st.session_state["business_question"] = value
            st.session_state["submitted_business_question"] = ""

        example_columns = st.columns(3)
        for index, example in enumerate(examples):
            example_columns[index % 3].button(
                example, key=f"question_example_{index}",
                on_click=choose_example, args=(example,), use_container_width=True,
            )
        st.session_state.setdefault("business_question", "")
        st.session_state.setdefault("submitted_business_question", "")
        with st.form("business_question_form", clear_on_submit=False):
            question = st.text_input(
                "Ask about collected records",
                key="business_question",
                placeholder="For example, what is the rental duration?",
            )
            submitted = st.form_submit_button("Ask", type="primary", key="ask_business_question")
        if submitted:
            st.session_state["submitted_business_question"] = question.strip()
        question_to_answer = st.session_state.get("submitted_business_question", "")

        if question_to_answer:
            result = answer(conn, question_to_answer, settings.selected_whatsapp_groups())
            rental_answer = result.get("rental_answer")
            marketing_answer = result.get("marketing_answer")
            verification_answer = result.get("verification_answer")
            structured_answer = rental_answer or marketing_answer or verification_answer
            if rental_answer:
                st.subheader("Rental quotation")
                if rental_answer["status"] == "insufficient_evidence" and not result.get("citations"):
                    st.info("I couldn't find sufficient information in the connected records to answer this question.")
                else:
                    st.write(result["answer"])
                st.caption(f"Evidence status: {rental_answer['status'].replace('_', ' ').title()}")
                if rental_answer.get("documents"):
                    for document in rental_answer["documents"]:
                        with st.container(border=True):
                            st.markdown(f"**Source document:** {document['title']} · Google Drive")
                            st.caption(f"Evidence status: {document['status'].replace('_', ' ').title()}")
                            available_fields = [field for field in document["fields"]
                                                if field.get("value") is not None]
                            for start_index in range(0, len(available_fields), 3):
                                field_columns = st.columns(3)
                                for index, field in enumerate(available_fields[start_index:start_index + 3]):
                                    with field_columns[index]:
                                        st.metric(field["label"], str(field["value"]))
                                        if field.get("status") in {"conflict", "invalid"}:
                                            st.caption(field.get("note") or "Source value needs review.")
                            with st.expander("View source evidence"):
                                for field in document["fields"]:
                                    if not field.get("evidence"):
                                        continue
                                    locations = [format_source_location(item)
                                                 for item in field.get("source_locations", [])]
                                    st.markdown(f"**{field['label']}**")
                                    st.write(field["evidence"])
                                    st.caption("; ".join(item for item in locations if item)
                                               if any(locations) else "Source location unavailable")
                                    if field.get("note"):
                                        st.caption(field["note"])
                else:
                    st.info("I couldn't find sufficient information in the connected records to answer this question.")
            elif marketing_answer:
                st.subheader("Marketing campaign budget")
                if marketing_answer["status"] == "insufficient_evidence" and not result.get("citations"):
                    st.info("I couldn't find sufficient information in the connected records to answer this question.")
                else:
                    st.write(result["answer"])
                st.caption(f"Evidence status: {marketing_answer['status'].replace('_', ' ').title()}")
                if marketing_answer.get("documents"):
                    for document in marketing_answer["documents"]:
                        with st.container(border=True):
                            st.markdown(f"**Source workbook:** {document['title']} · Google Drive")
                            st.caption(f"Campaign evidence status: {document['status'].replace('_', ' ').title()}")
                            calculations = document["calculations"]
                            planned_total = calculations.get("planned_total") or {}
                            actual_total = calculations.get("actual_total") or {}
                            variance_total = calculations.get("variance_total") or {}
                            total_columns = st.columns(3)
                            total_columns[0].metric("Planned total", planned_total.get("value") or "Unavailable")
                            total_columns[1].metric("Actual total", actual_total.get("value") or "Unavailable")
                            total_columns[2].metric("Variance · actual minus planned",
                                                    variance_total.get("value") or "Unavailable")
                            campaign_rows = []
                            for campaign in document["campaigns"]:
                                def displayed_value(value):
                                    if isinstance(value, dict):
                                        if value.get("status") == "invalid":
                                            return "Unverified source value: " + str(value.get("value", ""))
                                        return value.get("value") or "Not stated"
                                    return value or "Not stated"

                                campaign_rows.append({
                                    "Campaign": campaign["campaign"],
                                    "Channel": campaign.get("channel") or "Not stated",
                                    "Planned expenditure (extracted)": displayed_value(campaign.get("planned_expenditure")),
                                    "Actual expenditure (extracted)": displayed_value(campaign.get("actual_expenditure")),
                                    "Variance (calculated)": displayed_value(campaign.get("variance")),
                                    "Spending comparison": campaign.get("spending_status", "Insufficient data").replace("_", " ").title(),
                                    "Performance (source text)": campaign.get("performance") or "Not stated",
                                })
                            st.dataframe(campaign_rows, use_container_width=True, hide_index=True)
                            over = [item["campaign"] for item in document["campaigns"]
                                    if item.get("spending_status") == "over_budget"]
                            under = [item["campaign"] for item in document["campaigns"]
                                     if item.get("spending_status") == "under_budget"]
                            insight_columns = st.columns(2)
                            if over:
                                insight_columns[0].warning("Above planned expenditure: " + ", ".join(over))
                            else:
                                insight_columns[0].success("No above-budget campaigns identified")
                            if under:
                                insight_columns[1].info("Below planned expenditure: " + ", ".join(under))
                            else:
                                insight_columns[1].success("No below-budget campaigns identified")
                            st.caption("These comparisons describe expenditure and do not assess campaign effectiveness.")
                            with st.expander("View source evidence"):
                                for campaign in document["campaigns"]:
                                    locations = [format_source_location(item)
                                                 for item in campaign.get("source_locations", [])]
                                    st.markdown(f"**{campaign['campaign']} · Google Drive · {document['title']}**")
                                    st.write(campaign["evidence"])
                                    st.caption("Spreadsheet location: " + "; ".join(item for item in locations if item)
                                               if any(locations) else "Source location unavailable")
                                for label, calculation in (("Planned total", planned_total),
                                                           ("Actual total", actual_total),
                                                           ("Total variance", variance_total)):
                                    if not calculation:
                                        continue
                                    locations = [format_source_location(item)
                                                 for item in calculation.get("source_locations", [])]
                                    st.markdown(f"**{label} · calculated: {calculation.get('value') or 'Unavailable'}**")
                                    st.caption("Calculation uses: " + "; ".join(item for item in locations if item)
                                               if any(locations) else "Supporting worksheet locations unavailable")
                elif marketing_answer.get("note"):
                    st.warning(marketing_answer["note"])
            elif verification_answer:
                st.subheader("Customer verification summary")
                if verification_answer["status"] == "insufficient_evidence" and not result.get("citations"):
                    st.info("I couldn't find sufficient information in the connected records to answer this question.")
                else:
                    st.write(result["answer"])
                st.caption(f"Evidence status: {verification_answer['status'].replace('_', ' ').title()}")
                if verification_answer.get("documents"):
                    for document in verification_answer["documents"]:
                        with st.container(border=True):
                            st.markdown(f"**Source document:** {document['title']} · Google Drive")
                            st.caption(f"Record evidence status: {document['status'].replace('_', ' ').title()}")
                            overall = document["overall"]
                            overall_label = overall["status"].replace("_", " ").title()
                            if overall["status"] == "incomplete":
                                st.warning(f"Overall verification status: {overall_label}")
                            elif overall["status"] == "action_required":
                                st.error(f"Overall verification status: {overall_label}")
                            elif overall["status"] == "completed":
                                st.success(f"Overall verification status: {overall_label}")
                            else:
                                st.info(f"Overall verification status: {overall_label}")
                            st.caption(overall["basis"])
                            st.caption("Evidence coverage: " + document["evidence_coverage"])
                            if document.get("source_note"):
                                source_note = document["source_note"]
                                note_locations = "; ".join(
                                    label for label in
                                    (format_source_location(value)
                                     for value in source_note.get("source_locations", []))
                                    if label
                                ) or "Location unavailable"
                                st.warning(source_note["text"] + f" Source: {note_locations}.")
                            status_rows = []
                            for item in document["records"]:
                                if item["status"] == "unknown":
                                    status_text = "Unknown or not stated"
                                else:
                                    status_text = item["status"].title()
                                status_rows.append({
                                    "Document": item["document_name"],
                                    "Status stated in source": status_text,
                                    "Review note": item.get("notes") or "No note stated",
                                    "Source line": "; ".join(
                                        label for label in
                                        (format_source_location(value)
                                         for value in item.get("source_locations", []))
                                        if label
                                    ) or "Unavailable",
                                })
                            for item in document["records"]:
                                status_text = item["status"].replace("_", " ").title()
                                if item["status"] in {"pending", "rejected", "failed"}:
                                    st.warning(f"{item['document_name']}: {status_text}")
                                elif item["status"] == "verified":
                                    st.success(f"{item['document_name']}: {status_text}")
                                else:
                                    st.info(f"{item['document_name']}: {status_text} not stated")
                            st.dataframe(status_rows, use_container_width=True, hide_index=True)
                            with st.expander("View source evidence"):
                                st.markdown(f"**Source type:** Google Drive · `{document['title']}`")
                                verification_rows = []
                                for item in document["records"]:
                                    if item["status"] == "unknown":
                                        status_text = "Unknown or not stated"
                                    else:
                                        status_text = item["status"].title() + " (explicit in source)"
                                    locations = "; ".join(
                                        label for label in
                                        (format_source_location(value) for value in item.get("source_locations", []))
                                        if label
                                    ) or "Unavailable"
                                    verification_rows.append({
                                        "Document": item["document_name"],
                                        "Verification status": status_text,
                                        "Supporting evidence": item.get("evidence") or "Unavailable",
                                        "Notes (source text)": item.get("notes") or "Not stated",
                                        "Source location": locations,
                                        "Evidence status": ("Mapped to source line" if item.get("location_status") == "mapped"
                                                           else "Location unavailable"),
                                    })
                                st.dataframe(verification_rows, use_container_width=True, hide_index=True)
            else:
                if (not result.get("citations") and
                        result.get("answer", "").casefold().startswith("insufficient evidence")):
                    st.info("I couldn't find sufficient information in the connected records to answer this question.")
                else:
                    st.write(result["answer"])
            analytics = result.get("analytics")
            if analytics:
                if analytics["status"] == "complete_for_stored_records":
                    st.metric("Unanswered inbound emails in stored records", analytics["count"])
                else:
                    st.warning("The count is unavailable because Gmail data is absent or incomplete.")
                st.caption(analytics["criteria"])
                st.caption(analytics["coverage"])
            matching_counts = result.get("matching_counts")
            if matching_counts and not structured_answer:
                st.caption("Matching stored records by source (record counts, not customer/order counts)")
                st.dataframe([
                    {"Source": source.title(), "Exact text": counts["exact_literal"],
                     "Topic": counts["topic"], "Total": counts["total"]}
                    for source, counts in matching_counts.items()
                ], use_container_width=True, hide_index=True)
                st.caption("Shared text shows text overlap only; it does not establish a verified customer, order, or transaction relationship.")
            whatsapp_scope = result.get("whatsapp_scope")
            if whatsapp_scope and not structured_answer:
                if whatsapp_scope["status"] == "excluded_no_groups_selected":
                    st.info("WhatsApp results are unavailable because no groups are currently selected. Gmail and Drive results remain searchable.")
                st.caption(whatsapp_scope["note"])
            citations = result.get("citations", [])
            facets = prepare_facet_display(result.get("facets"))
            if not structured_answer and (citations or len(facets) > 1):
                st.caption("Retrieved evidence — review the source records before treating this as a business conclusion.")
                with st.expander("View source evidence", expanded=False):
                    evidence_by_id = {(row.get("source"), row.get("source_id")): row
                                      for row in result.get("evidence", []) if isinstance(row, dict)}

                    def render_citation(item, *, show_match_type=False, prepared=False):
                        display = item if prepared else prepare_citation_display(item)
                        if display is None:
                            return
                        if show_match_type:
                            if item.get("match_type") == "exact_literal":
                                st.subheader("Exact literal text matches")
                            elif item.get("match_type") == "topic":
                                st.subheader("Qualified topic matches")
                        with st.container(border=True):
                            st.markdown(f"**{display['source_type']}** · `{display['source_id']}`")
                            st.markdown(f"**{display['title']}**")
                            if item.get("interpretation_label"):
                                st.warning(item["interpretation_label"])
                                evidence_row = evidence_by_id.get((item.get("source"), item.get("source_id")), {})
                                for candidate in evidence_row.get("candidate_text", []):
                                    st.write(f"Candidate: {candidate}")
                            details = [f"Timestamp: {item.get('timestamp') or 'not available'}"]
                            if item.get("sender"):
                                details.append(f"Sender: {item['sender']}")
                            if item.get("thread_id"):
                                details.append(f"Gmail thread: {item['thread_id']}")
                            if item.get("group_context"):
                                details.append(f"WhatsApp group: {item['group_context']}")
                            if item.get("parent_id") and not item.get("thread_id") and not item.get("group_context"):
                                details.append(f"Parent ID: {item['parent_id']}")
                            st.caption(" · ".join(details))
                            st.markdown("**Evidence excerpt**")
                            st.write(display["excerpt"])
                            if display["locations"]:
                                st.caption("Source location · " + "; ".join(display["locations"]))
                            else:
                                st.caption("Source location unavailable")
                            if display["drive_link"]:
                                st.link_button("Open Drive file", display["drive_link"])

                    if len(facets) > 1:
                        st.subheader("Evidence by requested facet")
                        for facet in facets:
                            with st.expander(f"{facet['facet']} · {facet['status']}", expanded=True):
                                if facet["citations"]:
                                    for citation in facet["citations"]:
                                        render_citation(citation, prepared=True)
                                else:
                                    st.warning("Insufficient evidence for this facet.")
                        st.caption(f"{len(citations)} unique supporting source record(s) across facets.")
                    else:
                        previous_match_type = None
                        for item in citations:
                            if item.get("match_type") != previous_match_type:
                                previous_match_type = item.get("match_type")
                                render_citation(item, show_match_type=True)
                            else:
                                render_citation(item)

