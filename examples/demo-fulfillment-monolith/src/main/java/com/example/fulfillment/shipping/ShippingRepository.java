package com.example.fulfillment.shipping;

import org.springframework.data.jpa.repository.JpaRepository;

public interface ShippingRepository extends JpaRepository<ShippingLabel, Long> {
}
