"""
Logistic Regression and FM-LR Models.

Simple baseline models for CTR prediction.
"""

from __future__ import annotations

import torch
import torch.nn as nn


class LogisticRegression(nn.Module):
    """
    Logistic Regression for CTR Prediction.

    Simple linear model with embedding lookup for sparse features.
    """

    def __init__(
        self,
        sparse_feature_dims: list[int],
        dense_feature_dim: int = 0,
        l2_reg: float = 0.0,
    ):
        """
        Initialize LR model.

        Args:
            sparse_feature_dims: Vocabulary sizes for sparse features
            dense_feature_dim: Number of dense features
            l2_reg: L2 regularization strength
        """
        super().__init__()

        self.sparse_feature_dims = sparse_feature_dims
        self.dense_feature_dim = dense_feature_dim
        self.l2_reg = l2_reg

        # Sparse feature weights (embedding with dim=1)
        self.sparse_weights = nn.ModuleList([
            nn.Embedding(vocab_size, 1)
            for vocab_size in sparse_feature_dims
        ])

        # Dense feature weights
        if dense_feature_dim > 0:
            self.dense_weights = nn.Linear(dense_feature_dim, 1)
        else:
            self.dense_weights = None

        # Bias
        self.bias = nn.Parameter(torch.zeros(1))

        self._init_weights()

    def _init_weights(self):
        """Initialize weights."""
        for emb in self.sparse_weights:
            nn.init.zeros_(emb.weight)
        if self.dense_weights:
            nn.init.zeros_(self.dense_weights.weight)

    def forward(
        self,
        sparse_features: torch.Tensor,
        dense_features: torch.Tensor | None = None,
    ) -> dict[str, torch.Tensor]:
        """
        Forward pass.

        Args:
            sparse_features: (batch_size, num_sparse) - feature indices
            dense_features: (batch_size, num_dense) - feature values

        Returns:
            Dict with 'ctr' prediction
        """
        batch_size = sparse_features.size(0)

        # Start with bias
        logits = self.bias.expand(batch_size, 1)

        # Add sparse feature contributions
        for i, weight_emb in enumerate(self.sparse_weights):
            feat_idx = sparse_features[:, i]
            logits = logits + weight_emb(feat_idx)

        # Add dense feature contributions
        if self.dense_weights is not None and dense_features is not None:
            logits = logits + self.dense_weights(dense_features)

        pred = torch.sigmoid(logits)

        return {"ctr": pred.squeeze(-1)}

    def get_regularization_loss(self) -> torch.Tensor:
        """Get L2 regularization loss."""
        reg_loss = torch.tensor(0.0, device=next(self.parameters()).device)

        if self.l2_reg > 0:
            for emb in self.sparse_weights:
                reg_loss = reg_loss + self.l2_reg * torch.norm(emb.weight, 2)

        return reg_loss


class FactorizationMachineLR(nn.Module):
    """
    Factorization Machine + Logistic Regression.

    Combines:
    - 1st-order (LR): sum of feature weights
    - 2nd-order (FM): pairwise feature interactions
    """

    def __init__(
        self,
        sparse_feature_dims: list[int],
        dense_feature_dim: int = 0,
        embedding_dim: int = 8,
        l2_reg: float = 0.0,
    ):
        """
        Initialize FM-LR model.

        Args:
            sparse_feature_dims: Vocabulary sizes for sparse features
            dense_feature_dim: Number of dense features
            embedding_dim: Embedding dimension for FM
            l2_reg: L2 regularization strength
        """
        super().__init__()

        self.sparse_feature_dims = sparse_feature_dims
        self.dense_feature_dim = dense_feature_dim
        self.l2_reg = l2_reg

        # 1st-order weights
        self.sparse_weights = nn.ModuleList([
            nn.Embedding(vocab_size, 1)
            for vocab_size in sparse_feature_dims
        ])

        # 2nd-order embeddings
        self.sparse_embeddings = nn.ModuleList([
            nn.Embedding(vocab_size, embedding_dim)
            for vocab_size in sparse_feature_dims
        ])

        # Dense weights
        if dense_feature_dim > 0:
            self.dense_weights = nn.Linear(dense_feature_dim, 1)
            self.dense_embeddings = nn.Linear(dense_feature_dim, embedding_dim)
        else:
            self.dense_weights = None
            self.dense_embeddings = None

        # Bias
        self.bias = nn.Parameter(torch.zeros(1))

        self._init_weights()

    def _init_weights(self):
        """Initialize weights."""
        for emb in self.sparse_weights:
            nn.init.zeros_(emb.weight)
        for emb in self.sparse_embeddings:
            nn.init.xavier_uniform_(emb.weight)

    def forward(
        self,
        sparse_features: torch.Tensor,
        dense_features: torch.Tensor | None = None,
    ) -> dict[str, torch.Tensor]:
        """
        Forward pass.

        Args:
            sparse_features: (batch_size, num_sparse) - feature indices
            dense_features: (batch_size, num_dense) - feature values

        Returns:
            Dict with 'ctr' prediction
        """
        batch_size = sparse_features.size(0)

        # 1st-order: LR
        linear_out = self.bias.expand(batch_size, 1)

        for i, weight_emb in enumerate(self.sparse_weights):
            feat_idx = sparse_features[:, i]
            linear_out = linear_out + weight_emb(feat_idx)

        if self.dense_weights is not None and dense_features is not None:
            linear_out = linear_out + self.dense_weights(dense_features)

        # 2nd-order: FM
        emb_list = []
        for i, emb in enumerate(self.sparse_embeddings):
            feat_idx = sparse_features[:, i]
            emb_list.append(emb(feat_idx))

        if self.dense_embeddings is not None and dense_features is not None:
            emb_list.append(self.dense_embeddings(dense_features))

        # Stack embeddings: (batch, num_features, emb_dim)
        emb_stack = torch.stack(emb_list, dim=1)

        # FM interaction
        sum_square = torch.sum(emb_stack, dim=1) ** 2  # (batch, emb_dim)
        square_sum = torch.sum(emb_stack ** 2, dim=1)  # (batch, emb_dim)
        fm_out = 0.5 * torch.sum(sum_square - square_sum, dim=1, keepdim=True)

        # Combine
        logits = linear_out + fm_out
        pred = torch.sigmoid(logits)

        return {"ctr": pred.squeeze(-1)}

    def get_regularization_loss(self) -> torch.Tensor:
        """Get L2 regularization loss."""
        reg_loss = torch.tensor(0.0, device=next(self.parameters()).device)

        if self.l2_reg > 0:
            for emb in self.sparse_embeddings:
                reg_loss = reg_loss + self.l2_reg * torch.norm(emb.weight, 2)

        return reg_loss
