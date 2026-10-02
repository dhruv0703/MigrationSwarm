package com.example.fulfillment.billing;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class BillingAccount {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String accountNumber;

    protected BillingAccount() {
    }

    public BillingAccount(String accountNumber) {
        this.accountNumber = accountNumber;
    }
}
