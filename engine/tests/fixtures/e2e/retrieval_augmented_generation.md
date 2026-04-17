# Retrieval Augmented Generation

Retrieval augmented generation combines a language model with external knowledge sources.
The system embeds documents, retrieves relevant passages from a vector index, and then
asks the language model to answer using both the prompt and the retrieved evidence.
RAG is often used to ground transformer-based assistants in project-specific knowledge.

## Key Ideas

- Vector search improves recall over large document collections.
- Grounded answers can cite source passages instead of relying only on parametric memory.
- Retrieval works especially well when the underlying model is already a strong transformer.
