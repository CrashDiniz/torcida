"""Portuguese display names for the (English) team names in the TxODDS feed.

The feed sends national-team names in English ("England", "Spain"). This is a
BR product, so we render them in Portuguese for text and voice. Unknown names
fall through unchanged."""
from __future__ import annotations

PT_TEAMS = {
    "england": "Inglaterra", "spain": "Espanha", "france": "França",
    "germany": "Alemanha", "brazil": "Brasil", "portugal": "Portugal",
    "netherlands": "Holanda", "croatia": "Croácia", "belgium": "Bélgica",
    "italy": "Itália", "uruguay": "Uruguai", "mexico": "México",
    "united states": "Estados Unidos", "usa": "Estados Unidos",
    "morocco": "Marrocos", "japan": "Japão", "south korea": "Coreia do Sul",
    "korea republic": "Coreia do Sul", "switzerland": "Suíça",
    "denmark": "Dinamarca", "poland": "Polônia", "senegal": "Senegal",
    "australia": "Austrália", "ecuador": "Equador", "ghana": "Gana",
    "cameroon": "Camarões", "serbia": "Sérvia", "wales": "País de Gales",
    "iran": "Irã", "saudi arabia": "Arábia Saudita", "tunisia": "Tunísia",
    "costa rica": "Costa Rica", "canada": "Canadá", "qatar": "Catar",
    "argentina": "Argentina", "colombia": "Colômbia", "peru": "Peru",
    "chile": "Chile", "paraguay": "Paraguai", "nigeria": "Nigéria",
    "egypt": "Egito", "algeria": "Argélia", "ivory coast": "Costa do Marfim",
    "scotland": "Escócia", "ireland": "Irlanda", "sweden": "Suécia",
    "norway": "Noruega", "austria": "Áustria", "turkey": "Turquia",
    "türkiye": "Turquia", "ukraine": "Ucrânia", "greece": "Grécia",
    "czech republic": "Tchéquia", "russia": "Rússia", "china": "China",
    "new zealand": "Nova Zelândia", "panama": "Panamá",
}


def pt(name: str | None) -> str:
    """English feed team name -> Portuguese display name (unchanged if unknown)."""
    if not name:
        return name or "?"
    return PT_TEAMS.get(name.strip().lower(), name)
