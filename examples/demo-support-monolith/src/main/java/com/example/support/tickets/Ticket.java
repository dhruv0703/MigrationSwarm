package com.example.support.tickets;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class Ticket {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String subject;
    private String requesterId;
    private String status;

    protected Ticket() { }

    public Ticket(String subject, String requesterId) {
        this.subject = subject;
        this.requesterId = requesterId;
        this.status = "OPEN";
    }

    public String getSubject() { return subject; }
    public String getRequesterId() { return requesterId; }
    public String getStatus() { return status; }
}
