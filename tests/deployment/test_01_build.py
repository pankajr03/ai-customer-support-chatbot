"""Container build: the three images build from the working tree, as compose builds them."""

import json

from tests.deployment.conftest import TAG, Stack


def test_the_images_build_from_the_working_tree(built: Stack) -> None:
    for image in ("backend", "customer", "admin"):
        inspected = json.loads(
            built.docker("image", "inspect", f"support-chatbot-{image}:{TAG}").stdout
        )[0]
        config = inspected["Config"]
        assert config["User"] == "app", image  # never root
        assert config["Healthcheck"]["Test"][0] == "CMD", image  # the image's own check
        assert config["StopSignal"] == "SIGTERM", image


def test_the_backend_image_holds_no_model_and_no_secrets(built: Stack) -> None:
    listing = built.docker(
        "run", "--rm", "--entrypoint", "sh", f"support-chatbot-backend:{TAG}", "-c",
        "ls -A /models /app; ls /app/data; test ! -e /app/.env && echo no-env-file",
    ).stdout  # fmt: skip

    assert "no-env-file" in listing
    assert "model.safetensors" not in listing  # the model comes from the models volume
    assert "conversations.db" not in listing  # the database lives on the db volume
