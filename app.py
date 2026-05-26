"""Streamlit webové rozhranie pre Podcast Agent."""

from __future__ import annotations

import streamlit as st

from agent import OUTPUT_DIR, process_podcast

st.set_page_config(
    page_title="Podcast Agent",
    page_icon="🎙️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Inicializácia hodnôt v stave, ak neexistujú
if "url_input" not in st.session_state:
    st.session_state["url_input"] = ""
if "sections" not in st.session_state:
    st.session_state["sections"] = None
if "selected_model" not in st.session_state:
    st.session_state["selected_model"] = "gemini-2.5-flash"

st.title("🎙️ Podcast Agent")
# Podnadpis dynamicky ukazuje, ktorý model je práve aktívny
st.caption(f"Prepis podcastov do slovenčiny · Aktívny model: **{st.session_state['selected_model']}** · Gemini File API")

# Funkcia pre vyčistenie formulára
def clear_url():
    st.session_state["url_input"] = ""
    st.session_state["sections"] = None

# Prepínač modelov vo forme dvoch estetických tlačidiel pod nadpisom
st.write("### Prepni model Flash")
col_m1, col_m2, _ = st.columns([1.5, 1.5, 4])
with col_m1:
    if st.button("Použiť Gemini 2.5 Flash", type="secondary" if st.session_state["selected_model"] == "gemini-3.5-flash" else "primary", use_container_width=True):
        st.session_state["selected_model"] = "gemini-2.5-flash"
        st.rerun()
with col_m2:
    if st.button("Použiť Gemini 3.5 Flash", type="secondary" if st.session_state["selected_model"] == "gemini-2.5-flash" else "primary", use_container_width=True):
        st.session_state["selected_model"] = "gemini-3.5-flash"
        st.rerun()

st.divider()

# Vytvoríme stĺpce pre vstupné pole a tlačidlo reset
col_input, col_reset = st.columns([6, 1])

with col_input:
    url = st.text_input(
        "URL adresa podcastu (mp3)",
        placeholder="https://example.com/episode.mp3",
        key="url_input"
    )

with col_reset:
    st.markdown("<div style='padding-top: 28px;'></div>", unsafe_allow_html=True)
    if st.button("Nový súbor", on_click=clear_url, use_container_width=True):
        st.rerun()

reuse = st.checkbox(
    "Použiť už stiahnutý súbor (ak existuje v downloads/)",
    value=False,
)

if st.button("Spustiť prepis", type="primary", use_container_width=True):
    if not url.strip():
        st.error("Zadaj URL adresu podcastu.")
    else:
        status_box = st.empty()
        log_lines: list[str] = []

        def on_status(message: str) -> None:
            log_lines.append(message)
            status_box.info("\n\n".join(log_lines[-6:]))

        try:
            with st.spinner(f"Spracovávam podcast cez {st.session_state['selected_model']}, môže to trvať niekoľko minút…"):
                sections = process_podcast(
                    url.strip(),
                    model_name=st.session_state["selected_model"],
                    reuse_download=reuse,
                    status=on_status,
                )
            status_box.empty()
            st.success("Prepis je hotový.")

            st.session_state["sections"] = sections
            st.session_state["source_url"] = url.strip()
        except Exception as exc:
            status_box.empty()
            st.error(f"Chyba: {exc}")

sections = st.session_state.get("sections")
if sections:
    st.divider()
    
    jazyk = sections.get("JAZYK", "Neznámy")
    zhrnutie = sections.get("ZHRNUTIE", "Zhrnutie nebolo vygenerované alebo sa nezmestilo do limitu.")
    prepis = sections.get("PREPIS", sections.get("RAW_RESPONSE", "Prepis chýba."))

    st.subheader("Zhrnutie")
    st.markdown(f"**Jazyk:** {jazyk}")
    st.markdown(zhrnutie)

    st.subheader("Plný prepis")
    st.text_area(
        "Prepis",
        value=prepis,
        height=480,
        label_visibility="collapsed",
    )

    summary_text = f"Jazyk: {jazyk}\n\n{zhrnutie}"
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "Stiahnuť zhrnutie (.txt)",
            data=summary_text,
            file_name="zhrnutie.txt",
            mime="text/plain",
            use_container_width=True,
        )
    with col2:
        st.download_button(
            "Stiahnuť prepis (.txt)",
            data=prepis,
            file_name="prepis.txt",
            mime="text/plain",
            use_container_width=True,
        )

    st.caption(f"Súbory uložené aj v priečinku `{OUTPUT_DIR}/`.")
