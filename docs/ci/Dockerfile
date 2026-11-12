# An image for checking an Odoo project with odoo-typing in CI: Odoo's sources (not
# installed: mypy only reads them) and the plugin. Build it once per Odoo version:
#   docker build --build-arg ODOO_VERSION=18.0 -t odoo-typing:18 -f docs/ci/Dockerfile .
# and run mypy from the project directory mounted at /project:
#   docker run --rm -v "$PWD:/project" odoo-typing:18
FROM python:3.12-slim

ARG ODOO_VERSION=18.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
# A shallow, sparse checkout: no static/ or i18n/ (JS and translations mypy never reads).
RUN git clone --depth 1 --branch "$ODOO_VERSION" --filter=blob:none --no-checkout \
        https://github.com/odoo/odoo.git /opt/odoo \
    && git -C /opt/odoo sparse-checkout set --no-cone \
        '/*' '!/addons/*/static/' '!/addons/*/i18n/' '!/odoo/addons/*/static/' \
        '!/odoo/addons/*/i18n/' '!/doc/' \
    && git -C /opt/odoo checkout "$ODOO_VERSION"
RUN pip install --no-cache-dir odoo-typing

# The project's pyproject.toml points odoo_path at /opt/odoo/odoo and addons_path at
# /opt/odoo/addons plus its own addons (or uses odoo-typing-root with an overlay).
WORKDIR /project
CMD ["sh", "-c", "odoo-typing-root --check && mypy -p odoo.addons.${ADDON:-my_addon}"]
