package com.example.booking.shared;

import java.time.Instant;
import org.springframework.stereotype.Component;

@Component
public class AuditStamp {
    public Instant now() { return Instant.now(); }
    public void record(String event) { }
}
