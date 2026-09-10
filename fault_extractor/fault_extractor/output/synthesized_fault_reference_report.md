# Similar Fault Reference Report

## 1. Basis and Scope
- Matched historical references: **4** work order(s) from `output\reference_data_with_cause_inferenced.json`.
- Highest-similarity reference: **5001614276** (94.00%).
- Similarity scores are treated as reference-priority signals from time-series comparison; they do **not** prove an identical root cause.
- Cause reliability note: 2 case(s) have direct/non-marked causes and 2 case(s) contain inferred/inferenced cause text.

## 2. Matched Historical Cases
| Rank | Work order | Similarity | Fault | Key cause / diagnosis | Useful historical actions | Reported result |
|---:|---|---:|---|---|---|---|
| 1 | 5001614276 | 94.00% | P026 VCB tripped repeatedly | P026 intermittent VCB control abnormality involving APC controller, APC receiver, APCCR, RAMFR and VCBCR [inferred] | checked VCB; checked VCB circuit; replaced APC controller; replaced APC receiver; replaced APCCR | Function test passed, no repeat VCB fault log after leaving LOW DT. |
| 2 | 5001587808 | 90.00% | P107 VCB tripped repeatedly | ramping-down finish signal abnormal trigger outside neutral section | checked VCB; replaced ATR controller; replaced ATR receiver; function tested APC; function tested ATR | Function test passed |
| 3 | 5001505712 | 86.00% | P073 VCB tripped | ramping-down finish signal abnormal trigger outside neutral section | reset VCB; checked VCB; replaced ATR receiver; function tested ATR; function tested APC | Function test passed |
| 4 | 5001544878 | 81.00% | P073 VCB tripped | P073 transient VCB trip detection/control anomaly with no repeat fault found after reset [inferred] | reset VCB; function tested APC; function tested ATR; function tested VCB | Function normal during ride check, tests passed |

## 3. Synthesis for the New Fault
- New fault shows high similarity (0.81–0.94) to historical VCB tripping cases on R-Train units (T9, T36, T25).
- Historical evidence identifies 'VCB ramping-down finish signal abnormal trigger outside neutral section' as a direct cause in two cases (WO 5001587808, 5001505712).
- Top ranked case (WO 5001614276, similarity 0.94) cites an inferred 'intermittent VCB control abnormality' involving the APC/ATR/VCB control chain.
- Common phenomenon across references is VCB trip events, often resetting normally via T-cap but requiring root cause investigation.

## 4. Recommended Troubleshooting Path
1. Download and analyze TMS/ER logs to check for 'ramping-down finish signal abnormal trigger' and verify neutral section status during the trip.
2. Perform VCB reset and conduct a functional test of the VCB, APC, and ATR systems.
3. Inspect the VCB circuit and VCB ramping-down finish signal circuit for abnormalities.
4. If logs confirm 'ramping-down finish signal abnormal trigger,' prioritize inspection and testing of the ATR receiver and ATR controller.
5. If the fault persists or mimics the top-ranked case (WO 5001614276), systematically check components in the APC/ATR/VCB control chain.

## 5. Components, Actions, and Documents to Prioritize
- Cause-side focus: VCB ramping-down finish signal circuit, APC/ATR/VCB control chain, VCB trip detection/control circuit.
- Component/document focus: APC, ATR, VCB, VCB ramping-down finish signal circuit, ATR receiver, APC/ATR/VCB control chain, VCB trip detection/control circuit, APC controller.
- Historically used actions to review: function tested APC; function tested ATR; function tested VCB; checked VCB; replaced ATR receiver; reset VCB; checked VCB circuit; replaced APC controller.
- Referenced replacement part codes: 260072075, 260072027, 260072030, 260072060, 260072052, 29454-0067, 260072111, 260672083, 29454-0060, 26007-2025.
- WO 5001587808 and WO 5001505712: Provide direct evidence linking VCB trips to the 'ramping-down finish signal circuit' and detail successful replacement of ATR components.
- WO 5001614276: Reference for extensive troubleshooting involving the APC controller, APC receiver, and VCBCR if standard checks fail.
- WO 5001544878: Reference for transient faults where no repeat occurs after reset, suggesting a monitoring approach.

## 6. Cautions and Limits
- Causes marked as 'glm inferenced' in WO 5001614276 and WO 5001544878 are hypotheses, not confirmed root causes; validate with physical evidence.
- Do not replace parts based solely on similarity scores; use historical evidence to guide targeted testing first.
- Ensure functional tests are passed before returning the unit to service; historical cases show faults may not repeat immediately.
