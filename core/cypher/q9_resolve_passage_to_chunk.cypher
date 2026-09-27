// Q9 — resolve a retrieved passage back to its document id and chunk.
// The SDK's "[Source: ...]" passage prefix carries Document.path (the file
// path it was loaded from — absolute in file mode), not Document.id (the
// repo-relative id we assign), so match either. OPTIONAL MATCH so the
// document is still attributed even when the chunk text doesn't match.
// Params: $source, $passage_prefix (first ~120 chars of the passage, raw —
// not whitespace-normalised: CONTAINS is an exact substring test)
MATCH (d:Document)
WHERE d.id = $source OR d.path = $source
OPTIONAL MATCH (d)-[:PART_OF]->(c:Chunk)
WHERE c.text CONTAINS $passage_prefix
RETURN d.id AS doc_id, collect(c.id)[0] AS chunk_id
LIMIT 1
