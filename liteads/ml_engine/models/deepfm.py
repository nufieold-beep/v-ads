"""
DeepFM Model for CTR Prediction.

DeepFM combines:
- FM (Factorization Machine) for 2nd-order feature interactions
- DNN for higher-order feature interactions
"""

from __future__ import annotations

from typing import Any

import torch
import torch.nn as nn


class FMLayer(nn.Module):
    """
    Factorization Machine Layer.

    Computes 2nd-order feature interactions efficiently:
    sum_ij <v_i, v_j> x_i x_j = 0.5 * (sum(sum(v*x))^2 - sum(sum(v^2 * x^2)))
    """

    def __init__(self, reduce_sum: bool = True):
        super().__init__()
        self.reduce_sum = reduce_sum

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        """
        Args:
            inputs: (batch_size, num_features, embedding_dim)

        Returns:
            FM interaction output
        """
        # Square of sum
        sum_square = torch.sum(inputs, dim=1) ** 2  # (batch, emb_dim)
        # Sum of square
        square_sum = torch.sum(inputs ** 2, dim=1)  # (batch, emb_dim)

        # 0.5 * (square of sum - sum of square)
        output = 0.5 * (sum_square - square_sum)  # (batch, emb_dim)

        if self.reduce_sum:
            output = torch.sum(output, dim=1, keepdim=True)  # (batch, 1)

        return output


class DNN(nn.Module):
    """Deep Neural Network component."""

    def __init__(
        self,
        input_dim: int,
        hidden_units: list[int],
        dropout: float = 0.0,
        activation: str = "relu",
    ):
        super().__init__()

        layers = []
        prev_dim = input_dim

        for hidden_dim in hidden_units:
            layers.append(nn.Linear(prev_dim, hidden_dim))
            layers.append(nn.BatchNorm1d(hidden_dim))

            if activation == "relu":
                layers.append(nn.ReLU())
            elif activation == "leaky_relu":
                layers.append(nn.LeakyReLU())
            elif activation == "gelu":
                layers.append(nn.GELU())

            if dropout > 0:
                layers.append(nn.Dropout(dropout))

            prev_dim = hidden_dim

        self.dnn = nn.Sequential(*layers)
        self.output_dim = prev_dim

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.dnn(x)


class DeepFM(nn.Module):
    """
    DeepFM Model for CTR Prediction.

    Architecture:
    - Embedding layer for sparse features
    - Linear layer for 1st-order features
    - FM layer for 2nd-order interactions
    - DNN for higher-order interactions
    - Output: sigmoid(linear + fm + dnn)
    """

    def __init__(
        self,
        sparse_feature_dims: list[int],
        sparse_embedding_dims: int | list[int] = 8,
        dense_feature_dim: int = 0,
        sequence_feature_dims: dict[str, int] | None = None,
        sequence_embedding_dims: dict[str, int] | None = None,
        fm_k: int = 8,
        dnn_hidden_units: list[int] | None = None,
        dnn_dropout: float = 0.0,
        dnn_activation: str = "relu",
        l2_reg_embedding: float = 0.0,
        l2_reg_dnn: float = 0.0,
        output_dim: int = 1,
    ):
        """
        Initialize DeepFM.

        Args:
            sparse_feature_dims: List of vocabulary sizes for each sparse feature
            sparse_embedding_dims: Embedding dimension (int or list per feature)
            dense_feature_dim: Number of dense features
            sequence_feature_dims: Dict of sequence feature vocab sizes
            sequence_embedding_dims: Dict of sequence feature embedding dims
            fm_k: FM embedding dimension
            dnn_hidden_units: List of DNN hidden layer sizes
            dnn_dropout: Dropout rate for DNN
            dnn_activation: Activation function for DNN
            l2_reg_embedding: L2 regularization for embeddings
            l2_reg_dnn: L2 regularization for DNN
            output_dim: Output dimension (1 for binary classification)
        """
        super().__init__()

        self.sparse_feature_dims = sparse_feature_dims
        self.dense_feature_dim = dense_feature_dim
        self.l2_reg_embedding = l2_reg_embedding
        self.l2_reg_dnn = l2_reg_dnn

        dnn_hidden_units = dnn_hidden_units or [256, 128, 64]

        # Handle embedding dims
        if isinstance(sparse_embedding_dims, int):
            sparse_embedding_dims = [sparse_embedding_dims] * len(sparse_feature_dims)

        # Sparse embeddings
        self.sparse_embeddings = nn.ModuleList([
            nn.Embedding(vocab_size, emb_dim)
            for vocab_size, emb_dim in zip(sparse_feature_dims, sparse_embedding_dims)
        ])

        # Linear weights for 1st-order
        self.sparse_weights = nn.ModuleList([
            nn.Embedding(vocab_size, 1)
            for vocab_size in sparse_feature_dims
        ])

        # Dense linear
        if dense_feature_dim > 0:
            self.dense_linear = nn.Linear(dense_feature_dim, 1)
            self.dense_dnn_linear = nn.Linear(dense_feature_dim, fm_k)
        else:
            self.dense_linear = None
            self.dense_dnn_linear = None

        # Sequence embeddings (optional)
        self.sequence_embeddings = nn.ModuleDict()
        if sequence_feature_dims:
            for name, vocab_size in sequence_feature_dims.items():
                emb_dim = (sequence_embedding_dims or {}).get(name, fm_k)
                self.sequence_embeddings[name] = nn.EmbeddingBag(
                    vocab_size, emb_dim, mode="mean"
                )

        # FM layer
        self.fm = FMLayer(reduce_sum=True)

        # Calculate DNN input dimension
        total_emb_dim = sum(sparse_embedding_dims)
        if dense_feature_dim > 0:
            total_emb_dim += fm_k  # dense features projected to fm_k

        # DNN
        self.dnn = DNN(
            input_dim=total_emb_dim,
            hidden_units=dnn_hidden_units,
            dropout=dnn_dropout,
            activation=dnn_activation,
        )

        # Output layer
        self.output_layer = nn.Linear(dnn_hidden_units[-1], output_dim)

        # Bias
        self.bias = nn.Parameter(torch.zeros(1))

        # Initialize weights
        self._init_weights()

    def _init_weights(self):
        """Initialize model weights."""
        for emb in self.sparse_embeddings:
            nn.init.xavier_uniform_(emb.weight)
        for emb in self.sparse_weights:
            nn.init.zeros_(emb.weight)

        if self.dense_linear:
            nn.init.xavier_uniform_(self.dense_linear.weight)
        if self.dense_dnn_linear:
            nn.init.xavier_uniform_(self.dense_dnn_linear.weight)

    def forward(
        self,
        sparse_features: torch.Tensor,
        dense_features: torch.Tensor | None = None,
        sequence_features: dict[str, tuple[torch.Tensor, torch.Tensor]] | None = None,
    ) -> dict[str, torch.Tensor]:
        """
        Forward pass.

        Args:
            sparse_features: (batch_size, num_sparse_features) - indices
            dense_features: (batch_size, num_dense_features) - values
            sequence_features: Dict of (values, offsets) for sequence features

        Returns:
            Dict with 'ctr' prediction
        """
        batch_size = sparse_features.size(0)

        # 1st-order: Linear weights
        linear_out = self.bias.expand(batch_size, 1)

        for i, weight_emb in enumerate(self.sparse_weights):
            feat_idx = sparse_features[:, i]
            linear_out = linear_out + weight_emb(feat_idx)

        if self.dense_linear is not None and dense_features is not None:
            linear_out = linear_out + self.dense_linear(dense_features)

        # Get sparse embeddings for FM and DNN
        sparse_emb_list = []
        for i, emb in enumerate(self.sparse_embeddings):
            feat_idx = sparse_features[:, i]
            sparse_emb_list.append(emb(feat_idx))  # (batch, emb_dim)

        # Stack for FM: (batch, num_features, emb_dim)
        # Need same embedding dim for FM, so we use the first dim
        fm_emb_dim = sparse_emb_list[0].size(1)
        fm_inputs = []
        for emb in sparse_emb_list:
            if emb.size(1) == fm_emb_dim:
                fm_inputs.append(emb)
            else:
                # Project to same dim if needed
                fm_inputs.append(emb[:, :fm_emb_dim])

        fm_input = torch.stack(fm_inputs, dim=1)  # (batch, num_sparse, fm_dim)

        # 2nd-order: FM
        fm_out = self.fm(fm_input)  # (batch, 1)

        # DNN input: concatenate all embeddings
        dnn_input = torch.cat(sparse_emb_list, dim=1)  # (batch, total_sparse_emb)

        if self.dense_dnn_linear is not None and dense_features is not None:
            dense_emb = self.dense_dnn_linear(dense_features)
            dnn_input = torch.cat([dnn_input, dense_emb], dim=1)

        # DNN forward
        dnn_out = self.dnn(dnn_input)  # (batch, last_hidden)
        dnn_out = self.output_layer(dnn_out)  # (batch, 1)

        # Combine: linear + fm + dnn
        logits = linear_out + fm_out + dnn_out

        # Sigmoid for probability
        pred = torch.sigmoid(logits)

        return {"ctr": pred.squeeze(-1)}

    def get_regularization_loss(self) -> torch.Tensor:
        """Get L2 regularization loss."""
        reg_loss = torch.tensor(0.0, device=next(self.parameters()).device)

        # Embedding regularization
        if self.l2_reg_embedding > 0:
            for emb in self.sparse_embeddings:
                reg_loss = reg_loss + self.l2_reg_embedding * torch.norm(emb.weight, 2)

        # DNN regularization
        if self.l2_reg_dnn > 0:
            for name, param in self.dnn.named_parameters():
                if "weight" in name:
                    reg_loss = reg_loss + self.l2_reg_dnn * torch.norm(param, 2)

        return reg_loss
