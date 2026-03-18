# 🍄 Digital Taxonomist — Aquatic Hyphomycetes

**Automated morphotyping and classification of Ingoldian fungi using computer vision, knowledge graphs, and multimodal AI.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![Neo4j](https://img.shields.io/badge/Neo4j-5.x-green.svg)](https://neo4j.com/)
[![PyTorch](https://img.shields.io/badge/PyTorch-2.x-red.svg)](https://pytorch.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Overview

This project integrates:
- **Knowledge Graph** (Neo4j): 116 nodes / 216 relationships covering global taxonomy of aquatic hyphomycetes (Ingoldian fungi)
- **Computer Vision Pipeline**: Percentile-based preprocessing (v3) for TIFF microscopy image segmentation
- **Few-Shot Classification**: Siamese Vision Transformer (ViT-Small) for morphotype classification with limited labeled data (~4,600 images)
- **Multimodal AI**: LLaVA-Med/BiomedGPT via LangChain for automated taxonomic descriptions validated against the knowledge graph

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  TIFF Microscopy │────▶│  Preprocessing   │────▶│  Siamese ViT    │
│  Images (4600+)  │     │  Pipeline v3     │     │  Few-Shot CLF   │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
                         ┌──────────────────┐             ▼
                         │   Neo4j Graph    │◀───── Validation
                         │  116 nodes/216   │         Query
                         │  relationships   │────────────┐
                         └──────────────────┘            │
                                                         ▼
                         ┌──────────────────┐     ┌─────────────────┐
                         │   LangChain      │────▶│  VLM Multimodal │
                         │   Integration    │     │  LLaVA/BiomedGPT│
                         └──────────────────┘     └─────────────────┘
```

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Load knowledge graph into Neo4j
```bash
python src/graph/load_graph.py --json data/neo4j/aquatic_hyphomycetes_graph.json
```

### 3. Preprocess microscopy images
```bash
python src/preprocessing/segment_specimens.py --input data/raw/ --output data/processed/
```

### 4. Train Siamese ViT
```bash
python src/cv/train_siamese.py --config configs/siamese_vit_config.yaml
```

### 5. Run full pipeline
```bash
python src/main.py --image path/to/specimen.tiff --validate-graph
```

## Knowledge Graph Schema

| Node Type | Count | Description |
|-----------|-------|-------------|
| Phylum | 2 | Ascomycota, Basidiomycota |
| Class | 6 | Leotiomycetes, Dothideomycetes, Sordariomycetes, ... |
| Order | 5 | Helotiales (dominant), Pleosporales, Hypocreales, ... |
| Genus | 38 | Alatospora, Tetracladium, Tricladium, ... |
| Species | 15 | Key species with Brazilian distribution data |
| ConidialMorphotype | 5 | Stauroid, Scolecoid, Appendaged, Helicoid, Clavate |
| MorphologicalAttribute | 12 | Septation, branching, curvature, arm count, ... |
| EcologicalGroup | 4 | Ingoldian, Aero-aquatic, Terrestrial-aquatic, Submerged |
| GeographicRegion | 6 | Amazon, Atlantic Forest, Caatinga, Cerrado, ... |

### Relationship Types
`BELONGS_TO_PHYLUM`, `BELONGS_TO_CLASS`, `BELONGS_TO_ORDER`, `BELONGS_TO_GENUS`, `HAS_MORPHOTYPE`, `MEASURED_BY`, `OCCURS_IN`, `CLASSIFIED_AS`, `INHABITS`, `FOUND_ON`, `PRODUCES_ENZYME`, `USES_CONIDIOGENESIS`, `POLYPHYLETIC_IN`, `TARGETS_CLASSIFICATION_OF`, `INTEGRATES_WITH`, `FEEDS_INTO`

## Data Sources

- Fiuza et al. 2017 — Ingoldian fungi of Brazil: checklist and key (Phytotaxa 306)
- Baschien et al. 2013 — Molecular phylogeny of aquatic hyphomycetes (Leotiomycetes)
- Gulis et al. 2022 — ITS rDNA barcodes for aquatic hyphomycetes
- Duarte et al. 2016 — Biogeography of aquatic hyphomycetes
- Belliveau & Bärlocher 2005 — Multiple origins confirmed by molecular evidence

## Project Structure

```
digital-taxonomist/
├── src/
│   ├── graph/              # Neo4j graph operations
│   │   ├── load_graph.py   # JSON → Neo4j import
│   │   ├── query_engine.py # Cypher query templates
│   │   └── validate.py     # Graph coherence audit
│   ├── cv/                 # Computer vision models
│   │   ├── siamese_vit.py  # Siamese ViT-Small architecture
│   │   ├── train_siamese.py# Training loop
│   │   └── inference.py    # Single-image classification
│   ├── preprocessing/      # Image preprocessing
│   │   ├── segment_specimens.py  # TIFF → individual specimens
│   │   └── scale_calibration.py  # Scale bar detection
│   ├── utils/              # Shared utilities
│   └── main.py             # Full pipeline orchestrator
├── data/
│   ├── neo4j/              # Knowledge graph JSON
│   ├── raw/                # Raw TIFF microscopy images
│   └── processed/          # Segmented specimens
├── models/                 # Trained model checkpoints
├── notebooks/              # Jupyter analysis notebooks
├── tests/                  # Unit and integration tests
├── configs/                # YAML configuration files
├── docs/                   # Documentation
└── .github/workflows/      # CI/CD
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/new-genus-data`)
3. Commit changes (`git commit -m 'Add new genus morphological data'`)
4. Push to branch (`git push origin feature/new-genus-data`)
5. Open a Pull Request

## License

MIT License — see [LICENSE](LICENSE) for details.

## Citation

If you use this project in your research, please cite:
```bibtex
@software{digital_taxonomist_2026,
  title={Digital Taxonomist: Automated Classification of Aquatic Hyphomycetes},
  author={De Brito, Raquel},
  year={2026},
  url={https://github.com/your-username/digital-taxonomist}
}
```
