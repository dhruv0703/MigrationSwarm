package com.example.fulfillment.orders;

import com.example.fulfillment.billing.BillingAccount;
import com.example.fulfillment.shared.Address;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class Order {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String sku;
    private BillingAccount billingAccount;
    private Address destination;

    protected Order() {
    }

    public Order(String sku, BillingAccount billingAccount, Address destination) {
        this.sku = sku;
        this.billingAccount = billingAccount;
        this.destination = destination;
    }

    public String getSku() {
        return sku;
    }
}
