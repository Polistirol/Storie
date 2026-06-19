SYSTEM_PROMPT = """\
Sei il narratore in prima persona del testo fornito nel contesto (Adriano, \
rivolto a Marco). La tua unica fonte di verità sono i passaggi recuperati \
e quanto hai già detto in questa conversazione.

Regole:
1. Usa ESCLUSIVAMENTE informazioni presenti nel contesto recuperato e nella \
cronologia della chat. Non attingere a conoscenze pregresse: storia romana, \
biografie di imperatori, opere letterarie, né immagini generiche di \
«Adriano imperatore» o «Memorie di Yourcenar».
2. Stile, tono, lessico e ritmo: ricavali dai passaggi del testo sorgente — \
non da un registro narrativo che conosci già. Se il contesto è sobrio e \
intimo, sii sobrio e intimo; se è concreto e sensoriale, resta concreto.
3. Prima persona ("io", "mi", "mio"), come nel testo.
4. Se il contesto non basta per rispondere, dillo con onestà: non inventare \
fatti, date, persone o scene assenti dai passaggi.
5. Lunghezza proporzionata alla domanda: articola scene, sensazioni e \
relazioni presenti nel contesto, senza comprimere artificialmente i turni \
di seguito.
6. Non citare chunk_id, id del grafo o metadati tecnici.
7. Non usare elenchi puntati salvo richiesta esplicita.
8. In conversazione multi-turno: mantieni coerenza e non ripetere frasi o \
paragrafi già enunciati; aggiungi solo ciò che la nuova domanda chiede.
9. Riformula con naturalezza: non copiare verbatim lunghi passaggi, ma \
resta fedele al contenuto e alla voce del testo sorgente.
"""


def _user_turn_first(
    question: str,
    context: str,
    *,
    disable_thinking: bool,
) -> str:
    body = (
        "Passaggi recuperati dal testo sorgente e dal knowledge graph "
        "(unica fonte ammessa per fatti e stile):\n\n"
        f"{context}\n\n"
        "---\n"
        f"Domanda: {question}\n\n"
        "Rispondi in prima persona, con voce e registro ricavati dai passaggi sopra. "
        "Integra dettagli concreti presenti nel contesto (scene, sensazioni, relazioni). "
        "Non usare informazioni assenti dai passaggi."
    )
    if disable_thinking:
        body += " /no_think"
    return body


def _user_turn_follow_up(
    question: str,
    context: str,
    *,
    disable_thinking: bool,
) -> str:
    body = (
        "Passaggi aggiuntivi dal testo sorgente (unica fonte ammessa; integra "
        "dettagli nuovi, NON ripetere frasi già dette sopra a Marco):\n\n"
        f"{context}\n\n"
        "---\n"
        f"Domanda di seguito: {question}\n\n"
        "Rispondi in continuità con la conversazione, in prima persona, "
        "con voce e registro ricavati dai passaggi. Articola scene, sensazioni "
        "e relazioni presenti nel contesto; aggiungi solo ciò che la nuova domanda "
        "richiede, senza attingere a conoscenze esterne al testo."
    )
    if disable_thinking:
        body += " /no_think"
    return body


def build_messages(
    question: str,
    context: str,
    *,
    disable_thinking: bool = True,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _user_turn_first(
                question, context, disable_thinking=disable_thinking
            ),
        },
    ]


def build_chat_messages(
    question: str,
    context: str,
    history: list[dict[str, str]],
    *,
    disable_thinking: bool = True,
    follow_up: bool = False,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    turn_fn = _user_turn_follow_up if follow_up else _user_turn_first
    messages.append(
        {
            "role": "user",
            "content": turn_fn(question, context, disable_thinking=disable_thinking),
        }
    )
    return messages
