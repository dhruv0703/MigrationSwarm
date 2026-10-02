package com.example.fulfillment.shipping;

import com.example.fulfillment.billing.BillingAccount;
import com.example.fulfillment.shared.Address;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class ShippingLabel {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private BillingAccount account;
    private Address destination;

    protected ShippingLabel() {
    }

    public ShippingLabel(BillingAccount account, Address destination) {
        this.account = account;
        this.destination = destination;
    }
}
