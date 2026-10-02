package com.example.fulfillment.shared;

import java.math.BigDecimal;

public record Money(BigDecimal amount, String currency) {
}
