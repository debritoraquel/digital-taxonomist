#!/usr/bin/env python3
"""
genus_species_extractor.py -- Taxonomista Digital (validacao 2026-05-13)
========================================================================
Extracao RIGOROSA de epiteto (genero e especie) a partir de texto de
legenda de figura em PDFs de chaves taxonomicas (DPSFs inclusos).

Politica de rigor:
 1. Binomial estrito:       <Genus> <species>   (Capitalizado + minusculo)
 2. Genus deve estar no VOCABULARIO CONTROLADO (81+ generos do pipeline).
 3. Species deve ter >= 4 letras, sem digitos, sem pontuacao interna.
 4. Aceita genus sozinho quando binomial nao for encontrado
    (marcado como uncertain=True).
 5. Ignora capturas em secao de "material examinado", "etymology",
    "acknowledgements" (heuristica por palavras-chave no contexto).
 6. Suporta entradas de CHAVES DICOTOMICAS (DPSFs):
    "1a. Tetracladium marchalianum -- conidios tetraradiados"
    "5. Alatospora acuminata; esporos sigmoídeos"
 7. Extrai contexto ecologico inline (habitat, substrato, enzimas).

Vocabulario controlado:
 - Carrega dos 81+ generos diretamente do SQLite do pipeline
   (taxonomista_digital.db) via coluna nome/name.
 - Fallback: lista embutida no final do arquivo.

Formatos reconhecidos:
    "Fig. 3. Tetracladium marchalianum. Conidia..."
    "A, B: Alatospora acuminata De Wildeman"
    "Figura 12 – Anguillospora longissima (Sacc. & Syd.) Ingold"
    "(C) Clavariopsis aquatica"
    "Tricladium splendens Ingold — conidios tetrarradiados"
    "1a. Condylospora spumigena -- espuma de riachos"
    "Species: Tetracladium marchalianum, Alatospora acuminata"

Uso como modulo:
    from genus_species_extractor import EpithetExtractor
    x = EpithetExtractor.from_sqlite("data/taxonomista_digital.db")
    result = x.extract_from_caption(caption_text)
    # result = {"genus": "Tetracladium", "species": "marchalianum",
    #          "binomial": "Tetracladium marchalianum", "confidence": 0.95,
    #          "uncertain": False, "all_candidates": [...],
    #          "ecological_context": {"habitat": "lotic", ...}}
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

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

# -----------------------------------------------------------------------
# Regex para entradas de CHAVES DICOTOMICAS (DPSFs)
# -----------------------------------------------------------------------
# Formato: "1a." / "1b." / "5." / "12)" seguido do binomio ou genero
_DPSF_KEY_RE = re.compile(
    r"^\s*(\d{1,3})\s*(?:[ab]|[a-d])?\s*[.):\-–]\s*"
    r"([A-Z][a-z]{2,20})\s+([a-z][a-z\-]{3,25})",
    re.MULTILINE,
)

# Forma abreviada em chave: "T. marchalianum" depois do genero ter sido declarado
_ABBREVIATED_KEY_RE = re.compile(
    r"^\s*(\d{1,3})\s*(?:[ab]|[a-d])?\s*[.):\-–]\s*"
    r"([A-Z])\.\s*([a-z][a-z\-]{3,25})",
    re.MULTILINE,
)

# Lista de especies separada por virgula/ponto-e-virgula
# Aceita: "Species: A x, B y" / "Species encountered: A x, B y" / "Taxa: ..."
_SPECIES_LIST_RE = re.compile(
    r"(?:[Ss]pecies?|[Ee]sp[eé]cies?|[Tt]axa)"
    r"(?:\s+\w+)?\s*[:]\s*"
    r"([A-Z][a-z]{2,20}\s+[a-z][a-z\-]{3,25}"
    r"(?:\s*[,;]\s*[A-Z][a-z]{2,20}\s+[a-z][a-z\-]{3,25})*)",
)

# -----------------------------------------------------------------------
# Regex para contexto ecologico inline
# -----------------------------------------------------------------------
_ECO_HABITAT_RE = re.compile(
    r"\b(?:"
    r"(l[oó]tico|lotic|riacho|stream|river|corrent|running\s+water)"
    r"|(l[eê]ntico|lentic|lago|lake|lagoa|pond|still\s+water|standing\s+water)"
    r"|(estu[aá]r|estuarin|estuar)"
    r"|(marinho|marine|mangue|mangrove)"
    r"|(foam|espuma|foam\s+trap)"
    r"|(serrapilheira|leaf\s+litter|submerged\s+lea)"
    r"|(madeira\s+submersa|submerged\s+wood|woody\s+debris)"
    r"|(raiz|root|riparian\s+root)"
    r")\b",
    re.IGNORECASE,
)

# Termos ecologicos para substrate/enzymatic context
_ECO_ENZYME_RE = re.compile(
    r"\b(?:celulas[ea]|cellulas[ea]|pectinas[ea]|xylanas[ea]|lacase|laccase"
    r"|nitrato\s+redutase|nitrate\s+reductase|ligninolytic|decompos)\b",
    re.IGNORECASE,
)

# Mapeamento de termos para categorias canonicas
_HABITAT_MAP = {
    0: "lotic", 1: "lentic", 2: "estuarine", 3: "marine",
    4: "foam", 5: "leaf_litter", 6: "submerged_wood", 7: "riparian_roots",
}

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
    ecological_context: Dict[str, object] = field(default_factory=dict)
    key_entry_number: Optional[int] = None


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

    def _extract_ecological_context(self, text: str) -> Dict[str, object]:
        """Extract inline ecological context (habitat, substrate, enzyme hints)."""
        ctx: Dict[str, object] = {}
        m = _ECO_HABITAT_RE.search(text)
        if m:
            for i, grp in enumerate(m.groups()):
                if grp:
                    ctx["habitat_hint"] = _HABITAT_MAP[i]
                    break
        if _ECO_ENZYME_RE.search(text):
            ctx["enzymatic_hint"] = True
        return ctx

    def _expand_abbreviation(self, initial: str, epithet: str) -> Optional[str]:
        """Try to resolve 'T. marchalianum' -> 'Tetracladium' from known genera."""
        for g in self._known:
            if g[0] == initial:
                return g
        return None

    # -----------------------------------------------------------------
    def extract_from_key_entries(self, text: str) -> List[EpithetResult]:
        """
        Extrai TODOS os binomios de entradas de chave dicotomica (DPSF).
        Retorna lista ordenada pelo numero da entrada.

        Formato esperado (por linha):
            "1a. Tetracladium marchalianum -- conidios tetraradiados"
            "5)  Alatospora acuminata; substrato foliar submerso"
        """
        results: List[EpithetResult] = []
        # Rastreia o genero mais recente para expandir abreviaturas
        last_known_genus: Optional[str] = None

        for m in _DPSF_KEY_RE.finditer(text):
            num = int(m.group(1))
            genus, species = m.group(2), m.group(3)
            if not self._valid_binomial(genus, species):
                continue
            eco = self._extract_ecological_context(
                text[m.start():m.start() + 200]
            )
            last_known_genus = genus
            results.append(EpithetResult(
                genus=genus, species=species,
                binomial=f"{genus} {species}",
                confidence=0.90,
                uncertain=False,
                rule="dpsf-key-strict",
                all_candidates=[f"{genus} {species}"],
                context_snippet=text[m.start():m.start() + 180],
                ecological_context=eco,
                key_entry_number=num,
            ))

        # Tenta expandir abreviaturas usando o genero declarado
        for m in _ABBREVIATED_KEY_RE.finditer(text):
            num = int(m.group(1))
            initial, epithet = m.group(2), m.group(3)
            genus = (
                self._expand_abbreviation(initial, epithet)
                if last_known_genus and last_known_genus[0] == initial
                else self._expand_abbreviation(initial, epithet)
            )
            if genus and epithet not in _SPECIES_BLACKLIST:
                eco = self._extract_ecological_context(
                    text[m.start():m.start() + 200]
                )
                results.append(EpithetResult(
                    genus=genus, species=epithet,
                    binomial=f"{genus} {epithet}",
                    confidence=0.72,
                    uncertain=False,
                    rule="dpsf-key-abbreviated",
                    all_candidates=[f"{genus} {epithet}"],
                    context_snippet=text[m.start():m.start() + 180],
                    ecological_context=eco,
                    key_entry_number=num,
                ))

        results.sort(key=lambda r: r.key_entry_number or 0)
        return results

    def extract_species_list(self, text: str) -> List[EpithetResult]:
        """
        Extrai lista de especies do formato 'Species: A x, B y, C z'.
        Retorna uma EpithetResult por binomio valido encontrado.
        """
        results: List[EpithetResult] = []
        m = _SPECIES_LIST_RE.search(text)
        if not m:
            return results
        raw_list = m.group(1)
        for bm in _BINOMIAL_RE.finditer(raw_list):
            genus, species = bm.group(1), bm.group(2)
            if self._valid_binomial(genus, species):
                results.append(EpithetResult(
                    genus=genus, species=species,
                    binomial=f"{genus} {species}",
                    confidence=0.88,
                    uncertain=False,
                    rule="species-list",
                    all_candidates=[f"{genus} {species}"],
                    context_snippet=raw_list[:180],
                    ecological_context={},
                ))
        return results

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
                ecological_context=self._extract_ecological_context(caption),
            )

        # 1b. Tenta chave dicotomica (DPSF) antes de genus-only
        dpsf_hits = self.extract_from_key_entries(caption)
        if dpsf_hits:
            return dpsf_hits[0]

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
                    ecological_context=self._extract_ecological_context(caption),
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

    # Caption extraction tests
    caption_samples = [
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

    # DPSF key entry tests
    key_text = (
        "1a. Tetracladium marchalianum -- esporos tetraradiados em riachos\n"
        "1b. Alatospora acuminata; submerso em folhas lóticas\n"
        "2.  Flagellospora curvula (Ingold) forma sigmoide\n"
    )

    # Species list tests
    list_text = "Species encountered: Tricladium splendens, Lemonniera aquatica, Campylospora chaetocladia"

    ok = fail = 0
    print("-- Caption extraction --")
    for cap, expected in caption_samples:
        r = x.extract_from_caption(cap)
        got = r.binomial or r.genus
        status = "OK " if got == expected else "FAIL"
        if status == "OK ": ok += 1
        else: fail += 1
        print(f"  [{status}] expected={expected!r:40s} got={got!r:40s} rule={r.rule}")

    print("\n-- DPSF key entries --")
    key_hits = x.extract_from_key_entries(key_text)
    expected_keys = ["Tetracladium marchalianum", "Alatospora acuminata", "Flagellospora curvula"]
    for i, exp in enumerate(expected_keys):
        got = key_hits[i].binomial if i < len(key_hits) else None
        status = "OK " if got == exp else "FAIL"
        if status == "OK ": ok += 1
        else: fail += 1
        print(f"  [{status}] expected={exp!r:35s} got={got!r} rule={key_hits[i].rule if i < len(key_hits) else 'N/A'}")
        if i < len(key_hits) and key_hits[i].ecological_context:
            print(f"         eco={key_hits[i].ecological_context}")

    print("\n-- Species list --")
    list_hits = x.extract_species_list(list_text)
    exp_list = ["Tricladium splendens", "Lemonniera aquatica", "Campylospora chaetocladia"]
    for i, exp in enumerate(exp_list):
        got = list_hits[i].binomial if i < len(list_hits) else None
        status = "OK " if got == exp else "FAIL"
        if status == "OK ": ok += 1
        else: fail += 1
        print(f"  [{status}] expected={exp!r:35s} got={got!r}")

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
