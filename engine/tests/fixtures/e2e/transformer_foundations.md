# Transformer Foundations

Transformers are sequence models built around self-attention instead of recurrence.
The architecture combines token embeddings, multi-head attention, feed-forward layers,
and residual connections. Transformer models are the base for BERT, GPT, and many
modern machine translation systems.

## Key Ideas

- Self-attention lets each token attend to every other token in the sequence.
- Positional information is added so the model can reason about order.
- Large transformer models become useful general purpose language models after
  pretraining on broad corpora.
