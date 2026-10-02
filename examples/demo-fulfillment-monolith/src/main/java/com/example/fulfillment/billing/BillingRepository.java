package com.example.fulfillment.billing;

import org.springframework.data.jpa.repository.JpaRepository;

public interface BillingRepository extends JpaRepository<BillingAccount, Long> {
}
