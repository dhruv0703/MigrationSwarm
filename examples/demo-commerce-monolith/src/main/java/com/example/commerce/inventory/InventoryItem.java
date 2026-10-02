package com.example.commerce.inventory;

import com.example.commerce.shared.AuditStamp;
import jakarta.persistence.Embedded;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class InventoryItem {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String sku;
    private int availableQuantity;
    @Embedded
    private AuditStamp auditStamp;

    protected InventoryItem() {
    }

    public InventoryItem(String sku, int availableQuantity, AuditStamp auditStamp) {
        this.sku = sku;
        this.availableQuantity = availableQuantity;
        this.auditStamp = auditStamp;
    }

    public String getSku() {
        return sku;
    }

    public int getAvailableQuantity() {
        return availableQuantity;
    }

    public void reserve(int quantity) {
        if (quantity <= 0 || quantity > availableQuantity) {
            throw new IllegalArgumentException("Insufficient inventory");
        }
        availableQuantity -= quantity;
    }
}
