"""Streamlit webové rozhranie pre Podcast Agent."""

from __future__ import annotations

import streamlit as st

from agent import MODEL, OUTPUT_DIR, process_podcast

st.set_page_config(
    page_title="Podcast Agent",
    page_icon="🎙️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.title("🎙️ Podcast Agent")
st.caption(f"Prepis podcastov do slovenčiny · {MODEL} · Gemini File API")

# Inicializácia kľúča v session_state, ak neexistuje
if "url_value" not in st.session_state:
    st.session_state["url_value"] = ""

# Vytvoríme stĺpce pre vstupné pole a tlačidlo
col_input, col_reset = st.columns([6, 1])

with col_input:
    # Použijeme value naviazanú na stav, nie priamy parameter key
    url = st.text_input(
        "URL adresa podcastu (mp3)",
        placeholder="https://example.com/episode.mp3",
        value=st.session_state["url_value"]
    )
    # Hneď uložíme aktuálne napísanú hodnotu, aby sme ju nestratili pri reštarte
    st.session_state["url_value"] = url

with col_reset:
    st.markdown("<div style='padding-top: 28px;'></div>", unsafe_allow_html=True)
    if st.button("Nový súbor", use_container_width=True):
        # Tu bezpečne zmeníme hodnotu, pretože text_input ju nevlastní cez parameter key
        st.session_state["url_value"] = ""
        st.session_state["sections"] = None
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
            with st.spinner("Spracovávam podcast, môže to trvať niekoľko minút…"):
                sections = process_podcast(
                    url.strip(),
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
