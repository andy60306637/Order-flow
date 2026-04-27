---
name: factor-creator
description: Generates quantitative trading factors matching the current system architecture based on user requirements. Use when the user wants to implement a new research factor.
---

# Factor Creator Skill

This skill allows you to translate user ideas or logic into a fully compliant factor class within the `research` system architecture.

## Execution Flow

1. **Understand Requirements:** Determine the mathematical logic, whether the factor requires `Tick` data, what market direction (`sides`) it predicts, and what group (`group`) it belongs to.
2. **Review Implementation Details:** Read `references/factor_design.md` to understand how to correctly inherit from `FactorBase`, the required class attributes, and helper functions (like `safe_divide`, `klines_to_arrays`).
3. **Write Code:** Provide the implementation code in `research/factors.py` or another appropriate location, ensuring it is decorated with `@register_factor`.
4. **Validate:** Check that the array shapes match `len(klines)`, missing values are `np.nan`, and that Tick logic uses `_tick_metric` properly.

## Important Considerations

- Only implement what the user asks. Default to `requires_ticks = False` unless high-frequency order flow or tick details are specifically required.
- Do not import external packages beyond `numpy` unless absolutely necessary.
- Ensure the output array size is always equal to the input `klines` array size.

## References
- See [factor_design.md](references/factor_design.md) for factor templates, helper functions, and classification constants.
