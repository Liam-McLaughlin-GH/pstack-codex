# Upstream provenance

This Codex-native skill adapts pstack's `maintain-verification-skill` from the
pinned upstream commit
`53e579f1481697931fc44f5445171397cfa2b24b` in the `pstack` directory. It keeps
the feature-level source wave, coordinator-owned live pass, Doctor boundary,
cleanup/evidence rule, three outcomes, and documentation-versus-product
triage. It replaces runtime-specific delegation and control paths with
Codex-native read-only receipts and a local safe-patch helper.

The repository root retains the upstream MIT license. The adapted skill also
retains the original copyright and permission notice:

MIT License

Copyright (c) 2026 Lauren Tan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
