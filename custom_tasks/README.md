# Custom tasks

Drop additional task YAMLs here. They get loaded automatically when
`evalctl run` invokes `lm_eval` with `--include_path custom_tasks`.

This directory is COPYed into the runtime Docker image at build time
(`docker/Dockerfile`); anything here is available inside the container
without further configuration.
