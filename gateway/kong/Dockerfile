FROM kong:3.9.4

USER root

COPY plugins/pfp-authz \
  /usr/local/share/lua/5.1/kong/plugins/pfp-authz

ENV KONG_PLUGINS=bundled,pfp-authz

USER kong
