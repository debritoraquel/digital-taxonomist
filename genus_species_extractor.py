#!/usr/bin/env python3
"""
genus_species_extractor.py -- Taxonomista Digital (validacao 2026-04-20)
========================================================================
Extracao RIGOROSA de epiteto (genero e especie) a partir de texto de
legenda de figura em PDFs de chaves taxonomicas.

Politica de rigor:
 1. Binomial estrito:       <Genus> <species>   (Capitalizado + minusculo)
 2. Genus deve estar no VOCABULARIO CONTROLADO (81 generos do pipeline).
 3. Species deve ter >= 4 letras, sem digitos, sem pontuacao.
 4. Aceita genus sozinho quando binomial nao for encontrado
    (marcado como uncertain=True).
 5. Ignora capturas em secao de "material examinado", "etymology",
    "acknowledgements" (heuristica por palavras-chave no contexto).

Vocabulario controlado:
 - Carrega dos 81 generos diretamente do SQLite do pipeline
   (taxonomista_digital.db) via coluna nome/name.
 - Fallback: lista embutida no final do arquivo.

Formatos reconhecidos:
    "Fig. 3. Tetracladium marchalianum. Conidia..."
    "A, B: Alatospora acuminata De Wildeman"
    "Figura 12 – Anguillospora longissima (Sacc. & Syd.) Ingold"
    "(C) Clavariopsis aquatica"
    "Tricladium splendens Ingold — conidios tetrarradiados"

Uso como modulo:
    from genus_species_extractor import EpithetExtractor
    x = EpithetExtractor.from_sqlite("data/taxonomista_digital.db")
    result = x.extract_from_caption(caption_text)
    # result = {"genus": "Tetracladium", "species": "marchalianum",
    #          "binomial": "Tetracladium marchalianum", "confidence": 0.95,
    #          "uncertain": False, "all_candidates": [...]}
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

# -----------------------------------------------------------------------
# Regex de binomial estrito
# -----------------------------------------------------------------------
# Genus: maiuscula inicial, minusculas apos, minimo 3 letras
# Species: minusculas, minimo 4 letras
# Nao permitir digitos nem pontuacao interna
_BINOMIAL_RE = re.compile(
    r"\b([A-Z][a-z]{2,})\s+([a-z]{4,})\b"
)
_GENUS_ONLY_RE = re.compile(r"\b([A-Z][a-z]{2,})\b")

# Padrões que indicam INÍCIO de seção não confiável.
# Usamos regex de início de linha para não bloquear "(HKAS 123, holotype)" inline.
import re as _re
_BLOCK_SECTION_RE = _re.compile(
    r"(?:^|\n)\s*(?:"
    r"material\s+examined|material\s+examinado"
    r"|acknowledgements?|acknowledgments?|agradecimentos"
    r"|etymology|etimologia"
    r"|specimens?\s+examined"
    r"|holotype\s*:|holotipo\s*:"
    r"|isotype\s*:|isotipo\s*:"
    r")",
    _re.IGNORECASE,
)

# Epitetos espurios que a regex engole e precisam ser descartados
_SPECIES_BLACKLIST = {
    "figure", "figura", "figures", "figuras",
    "plate", "prancha", "plates",
    "chave", "chaves", "clave",
    "genus", "species", "specie", "generos",
    "conidia", "conidio", "conidios", "conidium",
    "mycelium", "mycelia",
    "substratum", "substrato",
    "habitat", "ecology", "ecologia",
    "descripcion", "description", "descricao",
    "type", "typus", "tipo",
    "note", "nota", "notes", "notas",
    "photo", "photos", "foto", "fotos",
    "scale", "escala",
    "based", "according",
    "following", "preceding",
    "growing", "producing",
    "collected", "isolated",
    "observed", "studied",
    "water", "stream", "river", "leaf", "leaves",
    "litter", "wood", "bark",
    # Palavras comuns inglês (4+ letras) que a regex engole no texto corrido
    "them", "they", "their", "there", "these", "those",
    "this", "that", "with", "from", "also", "only",
    "each", "both", "some", "many", "most", "more", "less",
    "used", "were", "have", "been", "than", "when", "then",
    "tree", "data", "gene", "genes", "locus", "loci",
    "taxa", "taxon", "clade", "clades", "node", "nodes",
    "which", "while", "after", "given", "using", "found",
    "known", "show", "such", "very", "even", "just", "thus",
    "other", "into", "onto", "upon", "over", "under",
    "shown", "based", "using", "among", "around", "about",
    "above", "below", "within", "without", "between",
    "through", "against", "during", "along", "across",
    "analysis", "support", "values", "bootstrap",
}

_GENUS_BLACKLIST_LEADING = {
    "Fig", "Figure", "Figura", "Plate", "Prancha", "Clave", "Chave",
    "Table", "Tabela", "Note", "Nota",
    "The", "This", "That", "These", "Those",
    "Conidia", "Conidium", "Conidio", "Conidios",
    "Leaf", "Stream", "Water", "River",
    "Scale", "Bar",
    "Family", "Familia", "Genus", "Species",
    "Order", "Class", "Kingdom",
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
    # Palavras comuns inglesas maiúsculas que a regex captura no texto corrido
    "Among", "Based", "Using", "These", "Those", "Which", "While",
    "After", "Before", "During", "Within", "Without", "Between",
    "Through", "Against", "Around", "About", "Above", "Below",
    "Under", "Over", "Into", "Onto", "Upon", "With", "From",
    "Also", "Only", "Both", "Each", "Most", "Some", "Many",
    "More", "Less", "Such", "Very", "Well", "Just", "Even",
    "Thus", "Then", "When", "Here", "There", "Where", "All",
    "Any", "New", "Old", "One", "Two", "Three", "Four", "Five",
    "Six", "Seven", "Eight", "Nine", "Ten", "Upon", "Since",
    "However", "Therefore", "Moreover", "Furthermore", "Although",
    # Termos de filogenética
    "Bootstrap", "Maximum", "Bayesian", "Parsimony", "Analysis",
    "Combined", "Concatenated", "Topology", "Phylogram", "Cladogram",
    "Support", "Values", "Matrix", "Dataset",
}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


@dataclass
class EpithetResult:
    genus: Optional[str]
    species: Optional[str]
    binomial: Optional[str]
    confidence: float
    uncertain: bool
    rule: str
    all_candidates: List[str] = field(default_factory=list)
    context_snippet: str = ""


class EpithetExtractor:
    def __init__(self, known_genera: Iterable[str], strict_vocab: bool = True):
        # Normaliza: sem acento, capitalizado
        self._known = {g.strip().capitalize() for g in known_genera if g.strip()}
        self._known_lower = {g.lower() for g in self._known}
        # strict_vocab=False: aceita qualquer binômio válido (para extração de treino)
        self._strict_vocab = strict_vocab

    @classmethod
    def from_sqlite(cls, db_path: str | Path, table: str = "genera",
                    name_col: str = "name", strict_vocab: bool = True) -> "EpithetExtractor":
        con = sqlite3.connect(str(db_path))
        try:
            cur = con.cursor()
            # tenta 'name' primeiro, depois 'nome' (legacy PT)
            for col in (name_col, "nome"):
                try:
                    cur.execute(f"SELECT {col} FROM {table}")
                    rows = [r[0] for r in cur.fetchall()]
                    if rows:
                        return cls(rows, strict_vocab=strict_vocab)
                except sqlite3.OperationalError:
                    continue
            raise RuntimeError(f"Nao consegui ler coluna de nome em {table}")
        finally:
            con.close()

    @classmethod
    def from_fallback(cls, strict_vocab: bool = True) -> "EpithetExtractor":
        return cls(_FALLBACK_GENERA, strict_vocab=strict_vocab)

    # -----------------------------------------------------------------
    def _blocked(self, text: str) -> bool:
        return bool(_BLOCK_SECTION_RE.search(text))

    def _valid_binomial(self, genus: str, species: str) -> bool:
        if species in _SPECIES_BLACKLIST:
            return False
        if genus in _GENUS_BLACKLIST_LEADING:
            return False
        if self._strict_vocab and genus.capitalize() not in self._known:
            return False
        return True

    def _valid_genus_only(self, genus: str) -> bool:
        if genus in _GENUS_BLACKLIST_LEADING:
            return False
        if self._strict_vocab:
            return genus.capitalize() in self._known
        return True

    # -----------------------------------------------------------------
    def extract_from_caption(self, caption: str) -> EpithetResult:
        """
        Aplica a politica de extracao rigorosa. Retorna sempre um EpithetResult,
        mesmo quando nada e encontrado (genus=species=None).
        """
        if not caption or not caption.strip():
            return EpithetResult(None, None, None, 0.0, True, "empty-caption")

        if self._blocked(caption):
            # Legenda provavelmente em secao de material examinado -- nao confiavel
            return EpithetResult(None, None, None, 0.0, True, "blocked-section",
                                 context_snippet=caption[:180])

        candidates: List[str] = []

        # 1. Binomial estrito
        best_binomial: Optional[tuple[str, str]] = None
        for m in _BINOMIAL_RE.finditer(caption):
            genus, species = m.group(1), m.group(2)
            if self._valid_binomial(genus, species):
                candidates.append(f"{genus} {species}")
                if best_binomial is None:
                    best_binomial = (genus, species)

        if best_binomial is not None:
            g, s = best_binomial
            return EpithetResult(
                genus=g, species=s,
                binomial=f"{g} {s}",
                confidence=0.95,
                uncertain=False,
                rule="binomial-strict",
                all_candidates=list(dict.fromkeys(candidates)),
                context_snippet=caption[:180],
            )

        # 2. Genus sozinho (uncertain=True)
        for m in _GENUS_ONLY_RE.finditer(caption):
            g = m.group(1)
            if self._valid_genus_only(g):
                return EpithetResult(
                    genus=g, species=None, binomial=None,
                    confidence=0.65,
                    uncertain=True,
                    rule="genus-only",
                    all_candidates=[g],
                    context_snippet=caption[:180],
                )

        # 3. Nada
        return EpithetResult(
            genus=None, species=None, binomial=None,
            confidence=0.0,
            uncertain=True,
            rule="no-match",
            context_snippet=caption[:180],
        )

    # -----------------------------------------------------------------
    def extract_from_pages(self, page_texts: Sequence[str], figure_index: int = 0) -> EpithetResult:
        """
        Quando a legenda isolada nao produzir binomial, usa o texto completo
        da pagina como contexto secundario. Util para 'chaves' onde o titulo
        da chave contem o genero.
        """
        for text in page_texts:
            r = self.extract_from_caption(text)
            if r.binomial is not None:
                return r
        # Fallback: genus only sobre texto concatenado
        full = "\n".join(page_texts)
        return self.extract_from_caption(full)


# -----------------------------------------------------------------------
# Lista fallback dos 81 generos (fallback caso o SQLite nao esteja disponivel)
# -----------------------------------------------------------------------
_FALLBACK_GENERA = [
    # tetrarradiada
    "Actinospora", "Alatospora", "Articulospora", "Beverwykella", "Campylospora",
    "Ceratosporella", "Clavariopsis", "Clavatospora", "Crucella", "Culicidospora",
    "Cylindrocarpon", "Dendrospora", "Dimorphospora", "Fontanospora", "Geniculospora",
    "Lateriramulosa", "Lemonniera", "Mycocentrospora", "Pseudoanguillospora",
    "Stenocladiella", "Tetrachaetum", "Tetracladium", "Tetranacrium", "Tricellula",
    "Tricladium", "Triscelophorus", "Tumularia", "Varicosporium", "Volucrispora",
    "Wiesneriomyces", "Ypsilina",
    # sigmoide
    "Anguillospora", "Centrospora", "Cylindrocladium", "Dactylella", "Diplocladiella",
    "Flagellospora", "Goniopila", "Gyoerffyella", "Isthmolongispora", "Lunulospora",
    "Margaritispora", "Mycofalcella", "Rhexoacrodictys", "Setosynnema",
    "Sigmoidea", "Spirosphaera", "Tricladiopsis",
    # ramificada
    "Actinocladium", "Articulospathulata", "Condylospora", "Curucispora",
    "Hidroceras", "Mycoenterolobium", "Phalangispora", "Speiropsis",
    "Synnematomyces", "Tripospermum", "Triposporium", "Trisulcosporium",
    "Vargamyces",
    # filamentosa
    "Camposporium", "Cylindrocarpon", "Dactylaria", "Helicomyces",
    "Heliscus", "Lemonniera", "Pleurophragmium", "Subulispora",
    "Taeniolella", "Trichothecium", "Xylomyces",
    # helicoidal
    "Helicodendron", "Helicomyces", "Helicosporium",
    "Helicoon", "Helicubis", "Neta",
    # esferica
    "Aquaphila", "Nawawia", "Pyricularia",
]


# -----------------------------------------------------------------------
# CLI / auto-test
# -----------------------------------------------------------------------
def _selftest():
    x = EpithetExtractor.from_fallback()
    samples = [
        ("Fig. 3. Tetracladium marchalianum. Conidia tetraradiate, 22 um long.",
         "Tetracladium marchalianum"),
        ("A, B: Alatospora acuminata De Wildeman",
         "Alatospora acuminata"),
        ("(C) Clavariopsis aquatica Ingold",
         "Clavariopsis aquatica"),
        ("Figura 12 - Anguillospora longissima (Sacc. & Syd.) Ingold",
         "Anguillospora longissima"),
        ("Material examined: Brazil, Amazonas, 2019.",
         None),
        ("Plate 2. Conidium shape in various species.",
         None),
        ("Tricladium splendens -- conidios tetrarradiados.",
         "Tricladium splendens"),
        ("Lemonniera sp. nov., isolated from submerged leaves.",
         "Lemonniera"),  # genus-only
    ]
    ok = fail = 0
    for cap, expected in samples:
        r = x.extract_from_caption(cap)
        got = r.binomial or r.genus
        status = "OK " if got == expected else "FAIL"
        if status == "OK ":
            ok += 1
        else:
            fail += 1
        print(f"  [{status}] expected={expected!r:40s} got={got!r:40s} rule={r.rule}")
    print(f"\nSelf-test: {ok}/{ok+fail} passed")
    return fail == 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(0 if _selftest() else 1)
    if len(sys.argv) < 2:
        print("Uso: python genus_species_extractor.py --selftest")
        print("     python genus_species_extractor.py <caption text>")
        sys.exit(1)
    x = EpithetExtractor.from_fallback()
    r = x.extract_from_caption(" ".join(sys.argv[1:]))
    import json
    print(json.dumps(asdict(r), indent=2, ensure_ascii=False))
