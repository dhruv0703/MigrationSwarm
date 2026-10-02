package com.example.commerce.notifications;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/notifications")
public class NotificationController {
    private final NotificationService service;

    public NotificationController(NotificationService service) {
        this.service = service;
    }

    @PostMapping("/order")
    public NotificationDto notifyOrder(
            @RequestParam String recipient,
            @RequestParam String orderReference) {
        return service.notifyOrder(recipient, orderReference);
    }
}
