# Model DNA

Inspect in Models or with `accs model dna MODEL_ID`; `--reset` removes that model's learned DNA, preserving metadata.

Dimensions include classification, planning, architecture, coding, debugging, testing, review, repair, tool use, structured output, instruction following, long context, documentation, reasoning, reliability and cost efficiency. Each includes a nullable value, sample count, provenance/source counts, update time and uncertainty. Unobserved means value null, samples zero and uncertainty unknown.

Observations use a 5% EWMA starting at 0.5. One pass moves it to 0.525, not certainty. Fewer than ten observations are limited; fewer than thirty moderate. These labels describe evidence volume, not calibrated statistical confidence. Specializations appear after five observations. Routing blends DNA after five observations with bounded influence on priors; mandatory capability, quality, privacy and budget gates remain.

Calibration and verified outcomes supply evidence. Verified receipts can add cost-efficiency evidence; a cheap failed task does not earn that signal. Latency, context, resource and price fields retain their metadata provenance and may stay unknown. Reset does not rewrite receipts or historical spend.

Mock DNA tests the pipeline, not a real model. Small fixtures do not establish production fitness or validate unmeasured dimensions.
