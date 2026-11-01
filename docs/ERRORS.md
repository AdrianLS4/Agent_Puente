# Catálogo de errores

| Código | Etapa | Sev. | Acción | Reintentos | Mensaje al usuario (ES) | Playbook |
|---|---|---|---|---|---|---|
| `E_AMOUNT_LIMIT` | intake | warn | `abort` | — | El monto supera el límite por operación. Divide la compra o contacta soporte. | Rechazar antes de cotizar. No se mueve dinero. |
| `E_NO_PROVIDERS` | sourcing | warn | `abort` | — | Ahora mismo no hay proveedores disponibles para este monto. Intenta en unos minutos. | Ningún proveedor pasó los filtros duros. Registrar motivos de rechazo por proveedor; no cobrar nada. |
| `E_FX_UNAVAILABLE` | sourcing | error | `abort` | — | No pudimos obtener la tasa de cambio. Intenta de nuevo en unos minutos. | Falló la fuente de tasa. Se usa el último valor guardado SOLO si tiene menos de fx_max_age; si no hay ninguno vigente, no cotizar (nunca inventar una tasa). |
| `E_QUOTE_EXPIRED` | sourcing | warn | `requote` | — | Tu cotización venció. Te mostramos una nueva; si ya pagaste, te devolvemos o ajustamos. | Si el cliente ya pagó VES y la deriva de tasa ≤ tolerancia → honrar. Si no → COMPENSATE. |
| `E_VES_TIMEOUT` | ves | info | `abort` | — | No recibimos tu pago en el tiempo previsto, la solicitud se cerró sin cargos. | AWAITING_VES venció. Nada que compensar: no se movió dinero. |
| `E_VES_AMOUNT_MISMATCH` | ves | error | `human_review` | — | El monto recibido no coincide con la cotización. Un operador lo revisará. | Pago parcial o excedido. No avanzar a PayPal. Humano decide devolver diferencia o completar. |
| `E_VES_DUPLICATE_REF` | ves | critical | `human_review` | — | Esa referencia de pago ya fue usada. Un operador revisará tu caso. | Referencia bancaria reutilizada = posible fraude. Bloquear y escalar. |
| `E_PP_AUTH` | paypal | error | `retry` | 1× (backoff 0s) | Estamos reconectando con PayPal, un momento… | 401: token vencido. El cliente pide token nuevo en cada llamada; reintentar 1 vez. Si persiste: credenciales mal → humano. |
| `E_PP_RATE_LIMIT` | paypal | warn | `retry` | 4× (backoff 2s) | PayPal está ocupado, reintentando… | 429: backoff exponencial (2,4,8,16 s). |
| `E_PP_NETWORK` | paypal | warn | `retry` | 3× (backoff 2s) | Problema de conexión con PayPal, reintentando… | Timeout/5xx. ANTES de reintentar una creación, consultar la BD/PayPal para no duplicar la orden. |
| `E_PP_UNPROCESSABLE` | paypal | error | `human_review` | — | PayPal rechazó la operación. Un operador la revisará. | 422 sin código conocido. Guardar el cuerpo completo en error_events y escalar. |
| `E_PP_NOT_FOUND` | paypal | error | `human_review` | — | No encontramos la orden en PayPal. Un operador lo revisará. | 404 al consultar/capturar: IDs desincronizados. No reintentar a ciegas. |
| `E_PP_INSTRUMENT_DECLINED` | paypal | warn | `wait` | — | (Proveedor) PayPal rechazó la fuente de fondos. Elige otra tarjeta/saldo y vuelve a aprobar. | INSTRUMENT_DECLINED: la orden sigue viva; el proveedor puede reaprobar con otro instrumento hasta el timeout. |
| `E_PP_NOT_APPROVED` | paypal | warn | `failover_provider` | — | El proveedor no completó el pago a tiempo. Te devolvemos tus Bs. y te ofrecemos otro proveedor. | No aprobó en provider_approval_timeout. Devolver VES, failed_orders+1 al proveedor, reabrir excluyéndolo (nuevo pago VES). |
| `E_PP_ALREADY_CAPTURED` | paypal | info | `treat_as_success` | — | Tu pago ya estaba confirmado. | ORDER_ALREADY_CAPTURED: idempotente. Leer la orden y continuar como éxito. |
| `E_PP_CAPTURE_PENDING` | paypal | warn | `wait` | — | PayPal está revisando el pago. Te avisamos apenas se confirme. | Captura en PENDING (revisión). NO entregar. Esperar webhook PAYMENT.CAPTURE.COMPLETED/DENIED. |
| `E_PP_AMOUNT_MISMATCH` | paypal | critical | `human_review` | — | Detectamos una diferencia en el monto pagado. Un operador lo revisará. | Monto capturado ≠ monto esperado. Congelar y escalar. |
| `E_PP_DUPLICATE_INVOICE` | paypal | info | `treat_as_success` | — | La factura ya existía. | Número de factura repetido: recuperar la existente por invoice_number en vez de crear otra. |
| `E_TRACKING_INVALID` | delivery | warn | `human_review` | — | No pudimos registrar el seguimiento del envío. Un operador lo revisará. | PayPal rechazó el tracker (carrier/número). Reintentar con carrier OTHER una vez; si falla, manual. |
| `E_DELIVERY_TIMEOUT` | delivery | error | `compensate` | — | El comercio no entregó a tiempo. Te devolvemos tus Bs. | Sin prueba de entrega al vencer delivery_deadline: reembolso PayPal (captura) + devolución VES. |
| `E_VES_REFUND_FAILED` | compensation | critical | `human_review` | — | Estamos procesando tu devolución manualmente. Te contactaremos. | La devolución en VES falló. Prioridad máxima en la cola humana. |
| `E_DISPUTE_HIGH_VALUE` | dispute | warn | `human_review` | — | Tu caso fue escalado a un especialista por su monto. | amount > dispute_auto_max_usd: el agente prepara el expediente, un humano decide. |
| `E_DISPUTE_DEADLINE_NEAR` | dispute | error | `human_review` | — | Tu caso fue escalado con prioridad. | Menos de dispute_min_hours_left para responder: humano con prioridad alta. |
| `E_DISPUTE_NO_EVIDENCE` | dispute | info | `accept_dispute` | — | Aceptamos el reclamo y te reembolsamos. | Sin prueba de entrega y monto bajo: aceptar es más barato que perder. |
| `E_DISPUTE_STRONG_EVIDENCE` | dispute | info | `contest_dispute` | — | Presentamos tu comprobante de entrega a PayPal. | Prueba fuerte (tracking entregado / comprobante digital con hash y fecha): contestar con evidencia. |
| `E_AGENT_ESCALATED` | dispute | info | `human_review` | — | Tu caso fue escalado a un especialista para revisarlo con cuidado. | El propio agente eligió 'human' dentro de las acciones permitidas (caso dudoso). |
| `E_GUARDRAIL_BLOCKED` | agent | warn | `human_review` | — | Tu caso fue escalado a un operador. | El LLM propuso una acción fuera de las permitidas por la política. Se ignora y se escala. |
| `E_IDEMPOTENT_REPLAY` | system | info | `treat_as_success` | — | Esa solicitud ya estaba registrada. | Misma idempotency_key: devolver la solicitud existente; no crear otra. |
| `E_INVALID_TRANSITION` | system | error | `human_review` | — | Estado inconsistente. Un operador lo revisará. | Transición no permitida por la máquina de estados: bug o evento fuera de orden. |
| `E_UNKNOWN` | system | error | `human_review` | — | Ocurrió un error inesperado. Un operador lo revisará. | Cualquier cosa no mapeada. Guardar traza completa. |
