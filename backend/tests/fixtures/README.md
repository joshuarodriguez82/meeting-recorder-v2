# Test fixtures

## `word97-sample.doc`

A **genuine** Word 97-2003 binary document (OLE2 compound file, magic
`D0CF11E0A1B11AE1`, with `WordDocument` and `1Table` streams). Its text
content is the single line:

> Test OLE file, saved as Word 97-2003 Document.

### Why a real file and not a synthetic one

`_extract_doc_ole2` in `services/document_service.py` walks the
`[MS-DOC]` FIB and piece table by hand. If that walk were tested against
a file this repo also generated, the test would only prove the parser
agrees with itself — a misreading of the spec would produce a fixture
that is wrong in exactly the same way, and the test would pass anyway.

This file was produced by Microsoft Word, so it is independent evidence
that the offsets and the compressed/uncompressed piece logic are right.

### Provenance

Taken from the [`olefile`](https://github.com/decalage2/olefile) project's
own test corpus (`tests/images/test-ole-file.doc`), which is distributed
under the BSD 2-Clause licence. Copied here unmodified.
