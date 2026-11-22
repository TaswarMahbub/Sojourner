"""Laptop shared-control publisher for schema 4.

docs/contracts.md, "Messages": shared control is the only publisher of motor
commands. Nothing in the simulator or on the rover moves unless this package
emits a datagram. Any change here that can publish non-zero motion needs a
Hardware/ML safety review before merge.

Modules, in the order the stop path depends on them:

    auth         canonical MAC encoding and pre-shared key loading
    durable      atomic whole-file replacement for persisted marks
    status_gate  publisher status gate and its persisted high-water marks
    session      session_id allocator and the UDP session sequence
    publisher    publisher duties: stop by default, motion only when authorized
    transport    UDP send and receive, no policy
    reset        local operator reset of a lost or corrupt persisted state

No gesture-to-velocity mapping exists in this package. The contract requires
that mapping to arrive in its own reviewed change.
"""
