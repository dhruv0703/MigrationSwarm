package com.example.commerce.orders;

import java.util.ArrayList;
import java.util.List;

import com.example.commerce.shared.AuditStamp;
import jakarta.persistence.CascadeType;
import jakarta.persistence.Embedded;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.OneToMany;

@Entity
public class CustomerOrder {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String customerId;
    @OneToMany(cascade = CascadeType.ALL)
    private List<OrderLine> lines = new ArrayList<>();
    @Embedded
    private AuditStamp auditStamp;

    protected CustomerOrder() {
    }

    public CustomerOrder(String customerId, List<OrderLine> lines, AuditStamp auditStamp) {
        this.customerId = customerId;
        this.lines = new ArrayList<>(lines);
        this.auditStamp = auditStamp;
    }

    public String getCustomerId() {
        return customerId;
    }

    public List<OrderLine> getLines() {
        return List.copyOf(lines);
    }
}
