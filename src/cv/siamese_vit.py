"""
Siamese Vision Transformer for few-shot classification of aquatic hyphomycete morphotypes.

Architecture: Twin ViT-Small encoders with shared weights, contrastive learning head.
Optimized for small datasets (~4600 microscopy images) with class imbalance.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import timm
from typing import Optional


class SiameseViT(nn.Module):
    """
    Siamese network with ViT-Small backbone for few-shot morphotype classification.
    
    The network learns an embedding space where conidial images of the same
    morphotype/genus are closer together than images of different types.
    
    Args:
        backbone: timm model name (default: 'vit_small_patch16_224')
        embedding_dim: output embedding dimension
        pretrained: use ImageNet pretrained weights
        freeze_layers: number of transformer blocks to freeze (transfer learning)
    """

    def __init__(
        self,
        backbone: str = "vit_small_patch16_224",
        embedding_dim: int = 256,
        pretrained: bool = True,
        freeze_layers: int = 6,
    ):
        super().__init__()

        self.encoder = timm.create_model(backbone, pretrained=pretrained, num_classes=0)
        encoder_dim = self.encoder.num_features

        # Freeze early transformer blocks for transfer learning
        if freeze_layers > 0:
            for i, block in enumerate(self.encoder.blocks):
                if i < freeze_layers:
                    for param in block.parameters():
                        param.requires_grad = False

        # Projection head: maps encoder output to embedding space
        self.projection = nn.Sequential(
            nn.Linear(encoder_dim, 512),
            nn.BatchNorm1d(512),
            nn.ReLU(inplace=True),
            nn.Dropout(0.3),
            nn.Linear(512, embedding_dim),
            nn.BatchNorm1d(embedding_dim),
        )

        # Classification head (for supervised fine-tuning)
        self.classifier: Optional[nn.Linear] = None

    def forward_one(self, x: torch.Tensor) -> torch.Tensor:
        """Encode a single image to embedding space."""
        features = self.encoder(x)
        embedding = self.projection(features)
        return F.normalize(embedding, p=2, dim=1)

    def forward(
        self, x1: torch.Tensor, x2: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Forward pass for a pair of images."""
        emb1 = self.forward_one(x1)
        emb2 = self.forward_one(x2)
        return emb1, emb2

    def init_classifier(self, num_classes: int):
        """Initialize classification head for supervised fine-tuning."""
        self.classifier = nn.Linear(self.projection[-1].num_features, num_classes)

    def classify(self, x: torch.Tensor) -> torch.Tensor:
        """Classify a single image (requires init_classifier)."""
        if self.classifier is None:
            raise RuntimeError("Call init_classifier(num_classes) first")
        embedding = self.forward_one(x)
        return self.classifier(embedding)

    def get_similarity(self, x1: torch.Tensor, x2: torch.Tensor) -> torch.Tensor:
        """Compute cosine similarity between two image batches."""
        emb1, emb2 = self.forward(x1, x2)
        return F.cosine_similarity(emb1, emb2)


class ContrastiveLoss(nn.Module):
    """
    Contrastive loss for Siamese network training.
    
    Pulls positive pairs (same morphotype) together and pushes
    negative pairs (different morphotypes) apart in embedding space.
    
    Args:
        margin: minimum distance for negative pairs
    """

    def __init__(self, margin: float = 1.0):
        super().__init__()
        self.margin = margin

    def forward(
        self, emb1: torch.Tensor, emb2: torch.Tensor, label: torch.Tensor
    ) -> torch.Tensor:
        """
        Args:
            emb1, emb2: embeddings from the siamese network
            label: 1 for same class (positive), 0 for different (negative)
        """
        dist = F.pairwise_distance(emb1, emb2)
        loss_pos = label * dist.pow(2)
        loss_neg = (1 - label) * F.relu(self.margin - dist).pow(2)
        return (loss_pos + loss_neg).mean()


class TripletLossWithMining(nn.Module):
    """
    Triplet loss with online hard negative mining.
    
    Better than contrastive loss for morphotype classification because
    it directly optimizes the relative ordering of distances, handling
    the continuous similarity between some morphotypes (e.g., triradiate
    vs tetraradiate stauroid forms).
    
    Args:
        margin: minimum distance gap between positive and negative pairs
    """

    def __init__(self, margin: float = 0.3):
        super().__init__()
        self.margin = margin

    def forward(
        self,
        anchor: torch.Tensor,
        positive: torch.Tensor,
        negative: torch.Tensor,
    ) -> torch.Tensor:
        dist_pos = F.pairwise_distance(anchor, positive)
        dist_neg = F.pairwise_distance(anchor, negative)
        loss = F.relu(dist_pos - dist_neg + self.margin)
        return loss.mean()


class GraphAwarePairSampler:
    """
    Generates training pairs using the Neo4j knowledge graph to create
    taxonomically-informed positive and negative examples.
    
    Strategy:
    - Positive pairs: same genus or closely related genera (same morphotype)
    - Hard negatives: different genus but same morphotype (tests fine-grained discrimination)
    - Easy negatives: different morphotype entirely
    
    This leverages the graph structure where genera sharing the same
    ConidialMorphotype node are morphologically similar but taxonomically distinct.
    """

    def __init__(self, graph_json_path: str):
        import json
        with open(graph_json_path) as f:
            data = json.load(f)
        
        self.genus_to_morphotype = {}
        self.morphotype_to_genera = {}
        
        for rel in data["relationships"]:
            if rel["type"] == "HAS_MORPHOTYPE":
                genus = rel["start_node_id"]
                morpho = rel["end_node_id"]
                self.genus_to_morphotype[genus] = morpho
                self.morphotype_to_genera.setdefault(morpho, []).append(genus)

    def get_hard_negative_genera(self, genus_id: str) -> list[str]:
        """Return genera with the same morphotype but different genus (hard negatives)."""
        morpho = self.genus_to_morphotype.get(genus_id)
        if not morpho:
            return []
        return [g for g in self.morphotype_to_genera.get(morpho, []) if g != genus_id]

    def get_easy_negative_genera(self, genus_id: str) -> list[str]:
        """Return genera with different morphotypes (easy negatives)."""
        morpho = self.genus_to_morphotype.get(genus_id)
        if not morpho:
            return []
        result = []
        for m, genera in self.morphotype_to_genera.items():
            if m != morpho:
                result.extend(genera)
        return result
