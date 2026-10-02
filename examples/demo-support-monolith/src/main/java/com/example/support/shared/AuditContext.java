package com.example.support.shared;

import org.springframework.stereotype.Component;

@Component
public class AuditContext {
    public void record(String event, String subject) { }
}
