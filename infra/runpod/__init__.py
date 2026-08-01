"""RunPod build-time GPU lifecycle for the DistilBERT fine-tune.

Three modules, deliberately layered so the teardown path never depends on the launch path:

- ``runpod_client``     transport, secrets, account spend guard, GPU prices, billing.
- ``terminate_runpod``  registry, name guard, orphan-safe reconcile, dry-run-by-default CLI.
- ``deploy_runpod``     the fine-tuning pod: image, bootstrap, atomic registry, guaranteed teardown.

``terminate_runpod`` imports only ``runpod_client``. It therefore keeps working when
``deploy_runpod`` is broken, half-edited, or absent -- which is exactly the state the machine
is in when a pod has leaked and somebody needs to stop the meter.
"""
