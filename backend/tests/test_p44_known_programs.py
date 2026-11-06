from app import address_labels as al

P44 = {
    "BULK2CNYn3mbgfYXEXiBBFxmmDChznpjQ4oRfce8w6R4": "Bulk",
    "onreuGhHHgVzMWSkj2oQDLDtvvGvoepBPkqyaubFcwe": "OnRe",
    "sVau1tXvayVWfotzm9Ahcv2qfnnfRWttt78BCnNC6dD": "Exponent Strategy Vaults",
    "HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq": "PancakeSwap CLMM",
    "AQU1FRd7papthgdrwPTTq5JacJh8YtwEXaBfKU3bTz45": "Aquifer",
    "QuaNtZsgYRe5Z9Bk4LZ4cTD9tbkVoyCNf1R2BN9bBDv": "Quantum",
    "goonuddtQRrWqqn5nFyczVKaie28f3kDkHWkHtURSLE": "GoonFi v2",
    "riptK81hDxhe5pW5jSzSM9iRA8azgEgLJ4dXkPtBS7j": "Riptide",
    "SV2EYYJyRz2YhfXwXnhNAevDEui5Q6yrfyo13WtupPF": "SolFi v2",
}
EXCLUDED = [
    "2sB4WkRwmkwNaidbRPFKzN2ZaV1enyajptupc4cMac4p",
    "85F1bj5k85LZxzHM35epKtHD5E11HcYsxLpV8VbyT6od",
    "raTEkEMFeDsvsELyQ5CMk4dS8Yhh3wyXXRL6QESd7p2",
    "raTeAJ92EGM17TBENyDNvrsjmhhujvaanXCFKuxF494",
]


def test_p44_programs_present_with_official_names():
    for pid, name in P44.items():
        assert al.SOLANA_KNOWN_PROGRAMS[pid] == name


def test_p44_unverified_programs_not_added():
    for pid in EXCLUDED:
        assert pid not in al.SOLANA_KNOWN_PROGRAMS


def test_p44_ids_are_valid_base58_32_bytes():
    for pid in P44:
        assert al.is_valid_solana_address(pid)
        assert al.solana_known_program(pid) == P44[pid]
