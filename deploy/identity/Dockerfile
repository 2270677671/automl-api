ARG KEYCLOAK_BASE_IMAGE=quay.nju.edu.cn/keycloak/keycloak:26.3.3@sha256:6a7217a100bd3e5de4063a27a538ef999a3c5a88c4b4ec0ffc0a642aee7b2597

FROM ${KEYCLOAK_BASE_IMAGE} AS builder

ENV KC_DB=postgres \
    KC_HEALTH_ENABLED=true \
    KC_METRICS_ENABLED=true

RUN /opt/keycloak/bin/kc.sh build

FROM ${KEYCLOAK_BASE_IMAGE}

COPY --from=builder /opt/keycloak/ /opt/keycloak/

ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
