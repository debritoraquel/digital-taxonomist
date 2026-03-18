"""
Digital Taxonomist — Full pipeline orchestrator.

Runs the complete classification pipeline:
1. Preprocess microscopy image (segment specimens)
2. Classify morphotype via Siamese ViT
3. Validate prediction against Neo4j knowledge graph
4. Generate taxonomic description (optional: VLM)

Usage:
    python src/main.py --image specimen.tiff --validate-graph
    python src/main.py --image specimen.tiff --model models/siamese_vit_best.pth
"""

import json
import logging
from pathlib import Path
from typing import Optional

import click
import torch
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def load_config(config_path: str = "configs/siamese_vit_config.yaml") -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def classify_specimen(
    image_path: str,
    model_path: Optional[str] = None,
    config_path: str = "configs/siamese_vit_config.yaml",
    validate_graph: bool = False,
    neo4j_uri: Optional[str] = None,
) -> dict:
    """
    Run the full classification pipeline on a single image.
    
    Returns a dict with: predicted_genus, morphotype, confidence,
    graph_validation (if enabled), and taxonomic_path.
    """
    config = load_config(config_path)

    # Step 1: Preprocess
    logger.info(f"Preprocessing: {image_path}")
    from preprocessing.segment_specimens import SpecimenSegmenter

    segmenter = SpecimenSegmenter(
        output_size=(config["data"]["image_size"], config["data"]["image_size"])
    )
    specimens = segmenter.segment(Path(image_path))

    if not specimens:
        return {"error": "No valid specimens found in image", "specimens_found": 0}

    logger.info(f"Found {len(specimens)} specimens")

    # Step 2: Classify (if model available)
    result = {
        "image": str(image_path),
        "specimens_found": len(specimens),
        "predictions": [],
    }

    if model_path and Path(model_path).exists():
        logger.info(f"Loading model: {model_path}")
        from cv.siamese_vit import SiameseViT

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = SiameseViT(
            backbone=config["model"]["backbone"],
            embedding_dim=config["model"]["embedding_dim"],
        )
        model.load_state_dict(torch.load(model_path, map_location=device))
        model.eval()
        model.to(device)

        logger.info("Model loaded, running inference...")
        # Inference would happen here with the trained model
        # For now, return placeholder
        result["model_loaded"] = True
    else:
        logger.warning("No model path provided or model not found. Skipping classification.")
        result["model_loaded"] = False

    # Step 3: Graph validation
    if validate_graph:
        uri = neo4j_uri or config.get("neo4j", {}).get("uri", "bolt://localhost:7687")
        logger.info(f"Validating against Neo4j graph at {uri}")
        try:
            from graph.query_engine import QueryEngine

            engine = QueryEngine(
                uri=uri,
                user=config.get("neo4j", {}).get("user", "neo4j"),
                password=config.get("neo4j", {}).get("password", "password"),
            )
            stats = engine.graph_stats()
            result["graph_stats"] = stats
            result["graph_connected"] = True
            engine.close()
        except Exception as e:
            logger.warning(f"Graph validation failed: {e}")
            result["graph_connected"] = False

    return result


@click.command()
@click.option("--image", required=True, help="Path to microscopy image (TIFF/PNG)")
@click.option("--model", default=None, help="Path to trained model checkpoint")
@click.option("--config", default="configs/siamese_vit_config.yaml", help="Config file")
@click.option("--validate-graph", is_flag=True, help="Validate predictions against Neo4j")
@click.option("--neo4j-uri", default=None, help="Neo4j connection URI")
@click.option("--output-json", default=None, help="Save results to JSON file")
def main(image, model, config, validate_graph, neo4j_uri, output_json):
    result = classify_specimen(
        image_path=image,
        model_path=model,
        config_path=config,
        validate_graph=validate_graph,
        neo4j_uri=neo4j_uri,
    )

    click.echo(json.dumps(result, indent=2, ensure_ascii=False))

    if output_json:
        with open(output_json, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        click.echo(f"\nResults saved to {output_json}")


if __name__ == "__main__":
    main()
