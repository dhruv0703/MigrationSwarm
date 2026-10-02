package com.example.commerce.customers;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class Customer {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String externalId;
    private String email;

    protected Customer() {
    }

    public Customer(String externalId, String email) {
        this.externalId = externalId;
        this.email = email;
    }

    public String getExternalId() {
        return externalId;
    }

    public String getEmail() {
        return email;
    }
}
