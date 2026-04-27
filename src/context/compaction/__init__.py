"""
Compaction sub-package - 4-layer progressive compression pipeline.

Layer 1: tool_trimmer   - Non-destructive tool result trimming
Layer 2: history_pruner - API round-based history pruning
Layer 3: history_summarizer - LLM-based history summarization
Layer 4: reactive_recovery  - Emergency recovery from prompt_too_long
"""
