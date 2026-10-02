package com.example.fulfillment.shared;

import java.time.Instant;

public record AuditRecord(String action, Instant occurredAt) {
}
