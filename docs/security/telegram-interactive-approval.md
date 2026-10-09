# Telegram interactive approval

Telegram inline buttons are a presentation and input convenience. They do not
grant authority and they do not execute work. The compact callback contains only
an action code and an opaque approval request ID; operation data, capabilities,
paths, identity, sessions, nonces, and credentials are never callback fields.

On every click, the Telegram adapter authenticates the allowlisted Telegram
actor and derives the channel and conversation from the Telegram update. The
runtime derives the active session and incarnation, reloads the request from the
canonical `ApprovalStore`, and invokes the same atomic transition primitive used
by `/approve`, `/reject`, and `/cancel`. It checks current status, TTL, integrity,
channel, conversation, requester session incarnation, actor rules, and the
two-person rule. Stale, replayed, malformed, cross-channel, cross-conversation,
and old-incarnation callbacks fail closed. Store locking remains the concurrency
authority; Telegram adds no in-memory approval lock.

The actor incarnation is re-read from the canonical session store, including
cross-process reset tombstones, immediately around the transition. A distinct
approver's session lock is held through `ApprovalService.transition()`, so a
concurrent reset and approval have one deterministic order. Cached or
client-supplied incarnation values cannot authorize a stale approver.

The requester's conversation identifies the request origin; it is not the
approval audience. For the two-person rule, a second approver may act from a
different private conversation only after independently passing the existing
channel authentication and allowlist gate. The canonical channel must match,
the requester still cannot self-approve, requester-owned actions remain bound
to the requester's exact session, and the requester's canonical incarnation
must still be current. This preserves private-chat two-person approval without
broadening approval to unauthenticated channel users.

Approval messages use a bounded, secret-scrubbed `ApprovalPresentation` view
rather than serializing `ApprovalRequest`. Slash-command fallbacks remain
available, and WhatsApp/CLI behavior remains on the shared transition rules.
The canonical scrubber recognizes complete generic, RSA, EC, DSA, OpenSSH, and
encrypted PKCS#8 private-key PEM blocks, including traditional encrypted PEM
headers. It scans at most 65,536 characters per candidate and 32 candidates;
key-like malformed blocks fail closed, while an incomplete marker mentioned in
ordinary documentation remains text.

An approval means only that the exact stored contract may proceed through the
existing `/exec` flow. It does not mean execution succeeded. The
`ApprovedExecutionContract`, transaction engine, verification and rollback stay
canonical, and `orchestrator.py` remains the sole execution engine. Telegram
cannot add targets or capabilities, bypass contract integrity, or enable remote
Git push.
