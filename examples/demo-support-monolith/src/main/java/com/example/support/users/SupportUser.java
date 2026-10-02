package com.example.support.users;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class SupportUser {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String externalId;
    private boolean active;

    protected SupportUser() { }

    public SupportUser(String externalId) {
        this.externalId = externalId;
        this.active = true;
    }

    public String getExternalId() { return externalId; }
    public boolean isActive() { return active; }
}
