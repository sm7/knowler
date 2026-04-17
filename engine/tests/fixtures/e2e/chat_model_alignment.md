# Chat Model Alignment

Chat models usually start from a pretrained transformer and then go through supervised
fine-tuning followed by reinforcement learning from human feedback. RLHF uses preference
data and a reward model to make the chat model more helpful, harmless, and aligned with
user intent.

## Key Ideas

- Supervised fine-tuning teaches the assistant style and response format.
- RLHF often uses preference comparisons and PPO style optimization.
- Alignment techniques build on top of transformer base models rather than replacing them.
